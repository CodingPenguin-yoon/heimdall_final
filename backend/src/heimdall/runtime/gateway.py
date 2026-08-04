from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from contextlib import suppress
from http.client import RemoteDisconnected
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from heimdall.deployments.models import Deployment
from heimdall.deployments.worker import RuntimeFailure, RuntimeProgress
from heimdall.runtime.docker import CandidateGeneration, DockerRuntime
from heimdall.runtime.models import RuntimeDeployment
from heimdall.runtime.process import CommandExecutionError, CommandResult, CommandRunner
from heimdall.runtime.repository import ProjectRuntime, RuntimeRepository


class RouteProbe(Protocol):
    def probe(
        self,
        url: str,
        *,
        timeout_seconds: float,
        heartbeat: Callable[[], None],
    ) -> None: ...


class HttpRouteProbe:
    def probe(
        self,
        url: str,
        *,
        timeout_seconds: float,
        heartbeat: Callable[[], None],
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            heartbeat()
            try:
                request = Request(url, method="GET")
                with urlopen(request, timeout=min(2, timeout_seconds)) as response:
                    if response.status < 500:
                        return
            except HTTPError as error:
                if error.code < 500:
                    return
            except (URLError, TimeoutError, RemoteDisconnected, ConnectionError):
                pass
            time.sleep(0.25)
        raise RuntimeFailure("ACTIVATION", "GATEWAY_ROUTE_PROBE_FAILED")


class NginxGatewayActivator:
    def __init__(
        self,
        repository: RuntimeRepository,
        docker: DockerRuntime,
        runner: CommandRunner,
        probe: RouteProbe,
        config_root: Path,
        *,
        docker_executable: str = "docker",
        image: str = "nginx:1.29-alpine",
        command_timeout_seconds: float = 120,
        route_timeout_seconds: float = 10,
    ) -> None:
        self._repository = repository
        self._docker = docker
        self._runner = runner
        self._probe = probe
        self._config_root = config_root.resolve()
        self._docker_executable = docker_executable
        self._image = image
        self._command_timeout_seconds = command_timeout_seconds
        self._route_timeout_seconds = route_timeout_seconds

    def is_active(self, deployment: Deployment) -> bool:
        current = self._repository.get(deployment.project_id)
        return current is not None and current.active_deployment_id == deployment.id

    def activate(
        self,
        deployment: Deployment,
        runtime: RuntimeDeployment,
        candidate: CandidateGeneration,
        progress: RuntimeProgress,
    ) -> None:
        gateway_name = _gateway_name(deployment.project_id.hex)
        directory = self._gateway_directory(deployment.project_id.hex)
        current_path = directory / "current.conf"
        last_good_path = directory / "last-good.config"
        candidate_path = directory / f"candidate-{deployment.id.hex}.config"
        _ensure_private_directory(directory)
        if not current_path.exists():
            _atomic_write(current_path, _default_config())
            _atomic_write(last_good_path, _default_config())

        observed_port = self._ensure_gateway(
            deployment,
            candidate.network_name,
            gateway_name,
            directory,
            progress,
        )
        stored = self._repository.ensure_gateway(deployment.project_id, gateway_name, observed_port)
        if stored.active_deployment_id == deployment.id:
            return
        self._connect_gateway(candidate.network_name, gateway_name, progress)
        _atomic_write(candidate_path, _nginx_config(deployment, runtime))
        self._test_config(candidate.network_name, candidate_path, progress)

        previous_config = current_path.read_text(encoding="utf-8")
        switched = False
        try:
            os.replace(candidate_path, current_path)
            switched = True
            self._reload(gateway_name, progress)
            for route in runtime.routes:
                self._probe.probe(
                    f"http://127.0.0.1:{stored.preview_port}{route.path}",
                    timeout_seconds=self._route_timeout_seconds,
                    heartbeat=progress.heartbeat,
                )
            previous = self._repository.activate(
                deployment.project_id,
                deployment.id,
                candidate.network_name,
                tuple(item.container_name for item in candidate.services),
                tuple(item.image_name for item in candidate.services),
            )
            _atomic_write(last_good_path, current_path.read_text(encoding="utf-8"))
            self._docker.promote_candidate(candidate)
            self._retire_previous(previous, candidate.network_name, gateway_name)
        except Exception as error:
            if switched:
                _atomic_write(current_path, previous_config)
                with suppress(RuntimeFailure):
                    self._reload(gateway_name, progress)
            if isinstance(error, RuntimeFailure):
                raise
            raise RuntimeFailure("ACTIVATION", "GATEWAY_ACTIVATION_FAILED") from error
        finally:
            candidate_path.unlink(missing_ok=True)

    def rollback_candidate(self, deployment: Deployment) -> None:
        current = self._repository.get(deployment.project_id)
        if current is not None and current.active_deployment_id == deployment.id:
            return
        directory = self._gateway_directory(deployment.project_id.hex)
        current_path = directory / "current.conf"
        last_good_path = directory / "last-good.config"
        gateway_name = _gateway_name(deployment.project_id.hex)
        if current_path.exists() and last_good_path.exists():
            marker = f"# deployment: {deployment.id}"
            if marker in current_path.read_text(encoding="utf-8"):
                _atomic_write(current_path, last_good_path.read_text(encoding="utf-8"))
                self._run_ignored(["exec", gateway_name, "nginx", "-s", "reload"])
        self._run_ignored(
            [
                "network",
                "disconnect",
                "--force",
                _candidate_network_name(deployment),
                gateway_name,
            ]
        )

    def _ensure_gateway(
        self,
        deployment: Deployment,
        network_name: str,
        gateway_name: str,
        directory: Path,
        progress: RuntimeProgress,
    ) -> int:
        inspected = self._run_ignored(
            ["inspect", "--format", "{{json .Config.Labels}}", gateway_name]
        )
        if inspected.returncode != 0:
            stored = self._repository.get(deployment.project_id)
            published = (
                f"127.0.0.1:{stored.preview_port}:8080" if stored is not None else "127.0.0.1::8080"
            )
            self._run(
                [
                    "run",
                    "--detach",
                    "--name",
                    gateway_name,
                    "--network",
                    network_name,
                    "--publish",
                    published,
                    "--restart",
                    "unless-stopped",
                    "--label",
                    "heimdall.managed=true",
                    "--label",
                    f"heimdall.project-id={deployment.project_id}",
                    "--label",
                    "heimdall.kind=gateway",
                    "--mount",
                    f"type=bind,src={directory},dst=/etc/nginx/conf.d,readonly",
                    self._image,
                ],
                progress,
                RuntimeFailure("ACTIVATION", "GATEWAY_START_FAILED", retryable=True),
            )
        else:
            try:
                labels = json.loads(inspected.stdout)
            except (json.JSONDecodeError, TypeError) as error:
                raise RuntimeFailure("ACTIVATION", "GATEWAY_NAME_CONFLICT") from error
            if not (
                isinstance(labels, dict)
                and labels.get("heimdall.managed") == "true"
                and labels.get("heimdall.project-id") == str(deployment.project_id)
                and labels.get("heimdall.kind") == "gateway"
            ):
                raise RuntimeFailure("ACTIVATION", "GATEWAY_NAME_CONFLICT")
        result = self._run(
            ["port", gateway_name, "8080/tcp"],
            progress,
            RuntimeFailure("ACTIVATION", "GATEWAY_PORT_UNAVAILABLE"),
        )
        return _published_port(result.stdout)

    def _connect_gateway(
        self, network_name: str, gateway_name: str, progress: RuntimeProgress
    ) -> None:
        result = self._run_ignored(["network", "connect", network_name, gateway_name])
        if result.returncode not in {0, 1}:
            raise RuntimeFailure("ACTIVATION", "GATEWAY_NETWORK_CONNECT_FAILED")
        progress.heartbeat()

    def _test_config(
        self, network_name: str, candidate_path: Path, progress: RuntimeProgress
    ) -> None:
        self._run(
            [
                "run",
                "--rm",
                "--network",
                network_name,
                "--mount",
                (f"type=bind,src={candidate_path},dst=/etc/nginx/conf.d/default.conf,readonly"),
                self._image,
                "nginx",
                "-t",
            ],
            progress,
            RuntimeFailure("ACTIVATION", "NGINX_CONFIG_INVALID"),
        )

    def _reload(self, gateway_name: str, progress: RuntimeProgress) -> None:
        self._run(
            ["exec", gateway_name, "nginx", "-s", "reload"],
            progress,
            RuntimeFailure("ACTIVATION", "GATEWAY_RELOAD_FAILED", retryable=True),
        )

    def _retire_previous(
        self,
        previous: ProjectRuntime | None,
        active_network_name: str,
        gateway_name: str,
    ) -> None:
        if (
            previous is None
            or previous.active_deployment_id is None
            or previous.active_network_name is None
            or previous.active_network_name == active_network_name
        ):
            return
        self._run_ignored(["network", "disconnect", previous.active_network_name, gateway_name])
        self._docker.retire_generation(
            previous.active_network_name,
            previous.active_container_names,
            previous.active_image_names,
            previous.active_deployment_id,
        )

    def _gateway_directory(self, project_hex: str) -> Path:
        return self._config_root / project_hex

    def _run(
        self,
        arguments: list[str],
        progress: RuntimeProgress,
        failure: RuntimeFailure | None = None,
    ):
        try:
            return self._runner.run(
                [self._docker_executable, *arguments],
                timeout_seconds=self._command_timeout_seconds,
                heartbeat=progress.heartbeat,
            )
        except CommandExecutionError as error:
            resolved = failure or RuntimeFailure(
                "ACTIVATION", "GATEWAY_COMMAND_FAILED", retryable=True
            )
            raise resolved from error

    def _run_ignored(self, arguments: list[str]):
        try:
            return self._runner.run(
                [self._docker_executable, *arguments],
                timeout_seconds=self._command_timeout_seconds,
                check=False,
            )
        except CommandExecutionError:
            return CommandResult(-1, "")


def _nginx_config(deployment: Deployment, runtime: RuntimeDeployment) -> str:
    generation = deployment.id.hex[:12]
    locations: list[str] = []
    for route in sorted(runtime.routes, key=lambda item: len(item.path), reverse=True):
        service = next(item for item in runtime.services if item.name == route.service)
        alias = f"{service.name}-g-{generation}"
        if route.path == "/":
            locations.append(_location("/", alias, service.internal_port))
        else:
            locations.append(_location(f"= {route.path}", alias, service.internal_port))
            locations.append(_location(f"^~ {route.path}/", alias, service.internal_port))
    return "\n".join(
        [
            f"# deployment: {deployment.id}",
            "server {",
            "    listen 8080;",
            "    server_name _;",
            *locations,
            "}",
            "",
        ]
    )


def _location(location: str, alias: str, port: int) -> str:
    return "\n".join(
        [
            f"    location {location} {{",
            f"        proxy_pass http://{alias}:{port};",
            "        proxy_set_header Host $host;",
            "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
            "        proxy_set_header X-Forwarded-Proto $scheme;",
            "    }",
        ]
    )


def _default_config() -> str:
    return "server { listen 8080; location / { return 503; } }\n"


def _gateway_name(project_hex: str) -> str:
    return f"hm-p{project_hex[:12]}-gateway"


def _candidate_network_name(deployment: Deployment) -> str:
    return f"hm-p{deployment.project_id.hex[:12]}-g{deployment.id.hex[:12]}"


def _published_port(output: str) -> int:
    for value in output.splitlines():
        _, separator, raw_port = value.strip().rpartition(":")
        if separator and raw_port.isdigit() and 1 <= int(raw_port) <= 65535:
            return int(raw_port)
    raise RuntimeFailure("ACTIVATION", "GATEWAY_PORT_UNAVAILABLE")


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeFailure("ACTIVATION", "GATEWAY_CONFIG_ROOT_INVALID")
    os.chmod(path, 0o700)


def _atomic_write(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)
