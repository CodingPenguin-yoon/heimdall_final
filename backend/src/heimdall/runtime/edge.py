from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from http.client import HTTPConnection
from pathlib import Path
from uuid import UUID

from heimdall.runtime.process import CommandExecutionError, CommandRunner

_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)
_DOCKER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_MAX_ROUTES = 1000
_MAX_CONFIG_BYTES = 1_048_576
_NO_ROUTES_CONFIG = "# No active public project routes.\n"
_TRANSACTION_JOURNAL_NAME = ".routing-transaction.json"


@dataclass(frozen=True, slots=True)
class EdgeRouteTarget:
    project_id: UUID
    hostname: str
    gateway_alias: str


@dataclass(frozen=True, slots=True)
class EdgeRouteChange:
    hostname: str
    enabled: bool
    previous_hostname: str | None = None


@dataclass(frozen=True, slots=True)
class _EdgeTransaction:
    previous: str
    candidate: str
    previous_hostname: str | None
    phase: _EdgeTransactionPhase


class _EdgeTransactionPhase(StrEnum):
    PREPARED = "PREPARED"
    COMMITTED = "COMMITTED"


@dataclass(frozen=True, slots=True)
class EdgeRuntimeError(RuntimeError):
    code: str
    retryable: bool = False
    uncertain: bool = False


class EdgeFinalizeRejectedError(RuntimeError):
    pass


class EdgeRouteProbe:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout_seconds: float = 10,
        interval_seconds: float = 0.25,
    ) -> None:
        if not host or not 1 <= port <= 65535:
            raise ValueError("edge probe endpoint is invalid")
        self._host = host
        self._port = port
        self._timeout_seconds = timeout_seconds
        self._interval_seconds = interval_seconds

    def management(self, hostname: str, heartbeat: Callable[[], None]) -> None:
        self._wait(hostname, "MANAGEMENT", heartbeat)

    def routed(self, hostname: str, heartbeat: Callable[[], None]) -> None:
        self._wait(hostname, "ROUTED", heartbeat)

    def not_found(self, hostname: str, heartbeat: Callable[[], None]) -> None:
        self._wait(hostname, "NOT_FOUND", heartbeat)

    def _wait(self, hostname: str, expectation: str, heartbeat: Callable[[], None]) -> None:
        deadline = time.monotonic() + self._timeout_seconds
        while time.monotonic() < deadline:
            heartbeat()
            try:
                connection = HTTPConnection(
                    self._host,
                    self._port,
                    timeout=min(2, self._timeout_seconds),
                )
                connection.request("GET", "/", headers={"Host": hostname})
                response = connection.getresponse()
                marker = response.getheader("X-Heimdall-Deployment-Id")
                management_marker = response.getheader("X-Heimdall-Management")
                status = response.status
                response.close()
                connection.close()
                if expectation == "MANAGEMENT" and status < 500 and management_marker == "true":
                    return
                if expectation == "ROUTED" and status < 500 and _valid_marker(marker):
                    return
                if expectation == "NOT_FOUND" and status == 404 and marker is None:
                    return
            except (OSError, TimeoutError):
                pass
            time.sleep(self._interval_seconds)
        code = {
            "MANAGEMENT": "EDGE_MANAGEMENT_PROBE_FAILED",
            "ROUTED": "EDGE_ROUTE_PROBE_FAILED",
            "NOT_FOUND": "EDGE_DEFAULT_ROUTE_PROBE_FAILED",
        }[expectation]
        raise EdgeRuntimeError(code, retryable=True)


class DockerEdgeConfigManager:
    def __init__(
        self,
        runner: CommandRunner,
        probe: EdgeRouteProbe,
        config_root: Path,
        management_hostname: str,
        *,
        docker_executable: str = "docker",
        nginx_image: str = "nginx:1.29-alpine",
        edge_network_name: str = "heimdall-edge",
        edge_container_name: str = "heimdall-edge-gateway",
        command_timeout_seconds: float = 120,
    ) -> None:
        if _HOSTNAME.fullmatch(management_hostname) is None:
            raise ValueError("management hostname must be canonical")
        if _DOCKER_NAME.fullmatch(edge_network_name) is None:
            raise ValueError("edge network name is invalid")
        if _DOCKER_NAME.fullmatch(edge_container_name) is None:
            raise ValueError("edge container name is invalid")
        self._runner = runner
        self._probe = probe
        self._config_root = config_root.resolve()
        self._management_hostname = management_hostname
        self._docker_executable = docker_executable
        self._nginx_image = nginx_image
        self._edge_network_name = edge_network_name
        self._edge_container_name = edge_container_name
        self._command_timeout_seconds = command_timeout_seconds

    def recover_interrupted(self) -> bool:
        _ensure_private_directory(self._config_root)
        lock_path = self._config_root / ".routing.lock"
        with lock_path.open("a+b") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                return self._validate_interrupted_locked()
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def apply(
        self,
        routes: Sequence[EdgeRouteTarget],
        change: EdgeRouteChange | None,
        *,
        heartbeat: Callable[[], None],
        fence: Callable[[], None],
        finalize: Callable[[], None],
        probe_all_routes: bool = False,
    ) -> None:
        rendered = render_edge_routes(routes)
        _ensure_private_directory(self._config_root)
        lock_path = self._config_root / ".routing.lock"
        with lock_path.open("a+b") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            candidate_path: Path | None = None
            switched = False
            finalized = False
            previous = _NO_ROUTES_CONFIG
            try:
                self._validate_interrupted_locked()
                heartbeat()
                fence()
                self._verify_edge(heartbeat)
                candidate_path = _private_temporary(self._config_root, rendered)
                self._test_config(candidate_path, heartbeat)
                fence()
                current_path = self._config_root / "public-routes.conf"
                if current_path.exists():
                    previous = current_path.read_text(encoding="utf-8")
                else:
                    _atomic_write(current_path, previous)
                transaction = _EdgeTransaction(
                    previous=previous,
                    candidate=rendered,
                    previous_hostname=(change.previous_hostname if change is not None else None),
                    phase=_EdgeTransactionPhase.PREPARED,
                )
                self._write_transaction(transaction)
                os.replace(candidate_path, current_path)
                candidate_path = None
                switched = True
                _fsync_directory(self._config_root)
                self._reload(heartbeat)
                self._probe_candidate(routes, change, heartbeat, probe_all_routes)
                fence()
                try:
                    finalize()
                except EdgeFinalizeRejectedError:
                    raise
                except Exception as error:
                    finalized = True
                    raise EdgeRuntimeError(
                        "EDGE_FINALIZE_UNCERTAIN",
                        uncertain=True,
                    ) from error
                finalized = True
                self._mark_transaction_committed(transaction)
                self._remove_transaction_journal()
            except Exception:
                if switched and not finalized:
                    try:
                        self._restore(rendered, previous, change, heartbeat)
                    except EdgeRuntimeError as restore_error:
                        raise EdgeRuntimeError(
                            "EDGE_STATE_UNCERTAIN",
                            retryable=False,
                            uncertain=True,
                        ) from restore_error
                raise
            finally:
                if candidate_path is not None:
                    candidate_path.unlink(missing_ok=True)
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _validate_interrupted_locked(self) -> bool:
        transaction = self._read_transaction()
        if transaction is None:
            return False
        current_path = self._config_root / "public-routes.conf"
        if not current_path.exists():
            raise EdgeRuntimeError(
                "EDGE_CONFIG_CHANGED_DURING_RECOVERY",
                uncertain=True,
            )
        current = current_path.read_text(encoding="utf-8")
        if current not in {transaction.previous, transaction.candidate}:
            raise EdgeRuntimeError(
                "EDGE_CONFIG_CHANGED_DURING_RECOVERY",
                uncertain=True,
            )
        return True

    def _read_transaction(self) -> _EdgeTransaction | None:
        journal_path = self._config_root / _TRANSACTION_JOURNAL_NAME
        if not journal_path.exists():
            return None
        try:
            payload = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise EdgeRuntimeError(
                "EDGE_TRANSACTION_JOURNAL_INVALID",
                uncertain=True,
            ) from error
        if not isinstance(payload, dict) or set(payload) != {
            "version",
            "previous",
            "candidate",
            "phase",
            "previous_hostname",
        }:
            raise EdgeRuntimeError("EDGE_TRANSACTION_JOURNAL_INVALID", uncertain=True)
        previous = payload["previous"]
        candidate = payload["candidate"]
        previous_hostname = payload["previous_hostname"]
        try:
            phase = _EdgeTransactionPhase(payload["phase"])
        except (TypeError, ValueError) as error:
            raise EdgeRuntimeError("EDGE_TRANSACTION_JOURNAL_INVALID", uncertain=True) from error
        if (
            payload["version"] != 2
            or not _bounded_config(previous)
            or not _bounded_config(candidate)
            or (
                previous_hostname is not None
                and (
                    not isinstance(previous_hostname, str)
                    or _HOSTNAME.fullmatch(previous_hostname) is None
                )
            )
        ):
            raise EdgeRuntimeError("EDGE_TRANSACTION_JOURNAL_INVALID", uncertain=True)
        return _EdgeTransaction(previous, candidate, previous_hostname, phase)

    def _write_transaction(self, transaction: _EdgeTransaction) -> None:
        payload = json.dumps(
            {
                "candidate": transaction.candidate,
                "phase": transaction.phase.value,
                "previous": transaction.previous,
                "previous_hostname": transaction.previous_hostname,
                "version": 2,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        try:
            _atomic_write(
                self._config_root / _TRANSACTION_JOURNAL_NAME,
                f"{payload}\n",
            )
        except OSError as error:
            raise EdgeRuntimeError(
                "EDGE_TRANSACTION_JOURNAL_WRITE_FAILED",
                retryable=True,
            ) from error

    def _mark_transaction_committed(self, transaction: _EdgeTransaction) -> None:
        self._write_transaction(
            _EdgeTransaction(
                previous=transaction.previous,
                candidate=transaction.candidate,
                previous_hostname=transaction.previous_hostname,
                phase=_EdgeTransactionPhase.COMMITTED,
            )
        )

    def _remove_transaction_journal(self) -> None:
        try:
            (self._config_root / _TRANSACTION_JOURNAL_NAME).unlink(missing_ok=True)
            _fsync_directory(self._config_root)
        except OSError as error:
            raise EdgeRuntimeError(
                "EDGE_TRANSACTION_JOURNAL_CLEAR_FAILED",
                uncertain=True,
            ) from error

    def _verify_edge(self, heartbeat: Callable[[], None]) -> None:
        network = self._run(
            [
                "network",
                "inspect",
                "--format",
                "{{json .Labels}}",
                self._edge_network_name,
            ],
            heartbeat,
            "EDGE_NETWORK_UNAVAILABLE",
        )
        try:
            network_labels = json.loads(network.stdout)
        except (json.JSONDecodeError, TypeError) as error:
            raise EdgeRuntimeError("EDGE_NETWORK_CONFLICT") from error
        if not _exact_labels(network_labels, "edge-network"):
            raise EdgeRuntimeError("EDGE_NETWORK_CONFLICT")

        edge = self._run(
            [
                "inspect",
                "--format",
                (
                    '{"labels":{{json .Config.Labels}},'
                    '"image":{{json .Config.Image}},'
                    '"running":{{json .State.Running}},'
                    '"networks":{{json .NetworkSettings.Networks}}}'
                ),
                self._edge_container_name,
            ],
            heartbeat,
            "EDGE_GATEWAY_UNAVAILABLE",
        )
        try:
            observation = json.loads(edge.stdout)
        except (json.JSONDecodeError, TypeError) as error:
            raise EdgeRuntimeError("EDGE_GATEWAY_CONFLICT") from error
        if (
            not isinstance(observation, dict)
            or not _exact_labels(observation.get("labels"), "edge-gateway")
            or observation.get("image") != self._nginx_image
            or observation.get("running") is not True
            or not isinstance(observation.get("networks"), dict)
            or self._edge_network_name not in observation["networks"]
        ):
            raise EdgeRuntimeError("EDGE_GATEWAY_CONFLICT")

    def _test_config(self, candidate_path: Path, heartbeat: Callable[[], None]) -> None:
        main_config = self._read_edge_file(
            "/etc/nginx/nginx.conf",
            heartbeat,
        )
        management_config = self._read_edge_file(
            "/tmp/heimdall-edge-conf/management.conf",
            heartbeat,
        )
        main_path = _private_temporary(self._config_root, main_config)
        management_path = _private_temporary(self._config_root, management_config)
        try:
            self._run(
                [
                    "run",
                    "--rm",
                    "--network",
                    self._edge_network_name,
                    "--mount",
                    (f"type=bind,src={main_path},dst=/etc/nginx/nginx.conf,readonly"),
                    "--mount",
                    (
                        f"type=bind,src={management_path},"
                        "dst=/tmp/heimdall-edge-conf/management.conf,readonly"
                    ),
                    "--mount",
                    (
                        f"type=bind,src={candidate_path},"
                        "dst=/etc/nginx/routes/public-routes.conf,readonly"
                    ),
                    self._nginx_image,
                    "nginx",
                    "-t",
                ],
                heartbeat,
                "EDGE_CONFIG_INVALID",
            )
        finally:
            main_path.unlink(missing_ok=True)
            management_path.unlink(missing_ok=True)

    def _read_edge_file(
        self,
        path: str,
        heartbeat: Callable[[], None],
    ) -> str:
        result = self._run(
            ["exec", self._edge_container_name, "cat", path],
            heartbeat,
            "EDGE_CONFIG_SNAPSHOT_FAILED",
        )
        if result.stdout_truncated or not result.stdout:
            raise EdgeRuntimeError("EDGE_CONFIG_SNAPSHOT_FAILED", retryable=True)
        return result.stdout

    def _reload(self, heartbeat: Callable[[], None]) -> None:
        self._verify_edge(heartbeat)
        self._run(
            ["exec", self._edge_container_name, "nginx", "-s", "reload"],
            heartbeat,
            "EDGE_RELOAD_FAILED",
        )

    def _probe_candidate(
        self,
        routes: Sequence[EdgeRouteTarget],
        change: EdgeRouteChange | None,
        heartbeat: Callable[[], None],
        probe_all_routes: bool,
    ) -> None:
        self._probe.management(self._management_hostname, heartbeat)
        if probe_all_routes:
            for route in routes:
                self._probe.routed(route.hostname, heartbeat)
        if change is None:
            return
        if change.enabled:
            self._probe.routed(change.hostname, heartbeat)
            if change.previous_hostname is not None and (
                change.previous_hostname != change.hostname
            ):
                self._probe.not_found(change.previous_hostname, heartbeat)
        else:
            self._probe.not_found(change.hostname, heartbeat)

    def _restore(
        self,
        candidate: str,
        previous: str,
        change: EdgeRouteChange | None,
        heartbeat: Callable[[], None],
    ) -> None:
        current_path = self._config_root / "public-routes.conf"
        if not current_path.exists() or current_path.read_text(encoding="utf-8") != candidate:
            raise EdgeRuntimeError("EDGE_CONFIG_CHANGED_DURING_RESTORE", uncertain=True)
        _atomic_write(current_path, previous)
        self._reload(heartbeat)
        self._probe.management(self._management_hostname, heartbeat)
        if change is not None:
            if change.previous_hostname is not None:
                self._probe.routed(change.previous_hostname, heartbeat)
            if change.enabled and change.hostname != change.previous_hostname:
                self._probe.not_found(change.hostname, heartbeat)
        self._remove_transaction_journal()

    def _run(
        self,
        arguments: list[str],
        heartbeat: Callable[[], None],
        code: str,
    ):
        try:
            return self._runner.run(
                [self._docker_executable, *arguments],
                timeout_seconds=self._command_timeout_seconds,
                heartbeat=heartbeat,
            )
        except CommandExecutionError as error:
            raise EdgeRuntimeError(code, retryable=True) from error


def render_edge_routes(routes: Sequence[EdgeRouteTarget]) -> str:
    if len(routes) > _MAX_ROUTES:
        raise EdgeRuntimeError("EDGE_ROUTE_LIMIT_EXCEEDED")
    hostnames: set[str] = set()
    sections = ["# Generated by Heimdall Routing Worker. Do not edit.\n"]
    for route in sorted(routes, key=lambda item: item.hostname):
        if _HOSTNAME.fullmatch(route.hostname) is None:
            raise EdgeRuntimeError("EDGE_ROUTE_SNAPSHOT_INVALID")
        if _DOCKER_NAME.fullmatch(route.gateway_alias) is None:
            raise EdgeRuntimeError("EDGE_ROUTE_SNAPSHOT_INVALID")
        if route.hostname in hostnames:
            raise EdgeRuntimeError("EDGE_ROUTE_SNAPSHOT_INVALID")
        hostnames.add(route.hostname)
        upstream = f"hm_edge_p{route.project_id.hex[:12]}"
        sections.append(
            "\n".join(
                [
                    f"upstream {upstream} {{",
                    f"    zone {upstream} 64k;",
                    "    resolver 127.0.0.11 valid=10s ipv6=off;",
                    f"    server {route.gateway_alias}:8080 resolve;",
                    "}",
                    "",
                    "server {",
                    "    listen 80;",
                    f"    server_name {route.hostname};",
                    "",
                    "    location / {",
                    f"        proxy_pass http://{upstream};",
                    "        proxy_http_version 1.1;",
                    '        proxy_set_header Connection "";',
                    "        proxy_set_header Host $host;",
                    "        proxy_set_header X-Forwarded-For $remote_addr;",
                    "        proxy_set_header X-Forwarded-Host $host;",
                    "        proxy_set_header X-Forwarded-Proto $scheme;",
                    "    }",
                    "}",
                    "",
                ]
            )
        )
    rendered = "\n".join(sections)
    if len(rendered.encode("utf-8")) > _MAX_CONFIG_BYTES:
        raise EdgeRuntimeError("EDGE_ROUTE_CONFIG_TOO_LARGE")
    return rendered


def _exact_labels(labels: object, kind: str) -> bool:
    return (
        isinstance(labels, dict)
        and labels.get("heimdall.managed") == "true"
        and labels.get("heimdall.kind") == kind
    )


def _valid_marker(value: str | None) -> bool:
    if value is None:
        return False
    try:
        UUID(value)
    except (ValueError, AttributeError):
        return False
    return True


def _bounded_config(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return len(value.encode("utf-8")) <= _MAX_CONFIG_BYTES
    except UnicodeEncodeError:
        return False


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise EdgeRuntimeError("EDGE_CONFIG_ROOT_INVALID")
    os.chmod(path, 0o700)


def _private_temporary(directory: Path, value: str) -> Path:
    descriptor, raw_path = tempfile.mkstemp(prefix=".public-routes-", suffix=".tmp", dir=directory)
    path = Path(raw_path)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def _atomic_write(path: Path, value: str) -> None:
    temporary = _private_temporary(path.parent, value)
    try:
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
