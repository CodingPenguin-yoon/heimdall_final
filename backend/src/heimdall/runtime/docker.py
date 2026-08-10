from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from http.client import RemoteDisconnected
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID

from heimdall.deployments.models import Deployment, DeploymentStatus
from heimdall.deployments.worker import RuntimeFailure, RuntimeProgress
from heimdall.runtime.logs import (
    SERVICE_LOG_MAX_LINE_BYTES,
    SERVICE_LOG_TAIL,
    ServiceLogError,
    ServiceLogLine,
    ServiceLogSnapshot,
    ServiceLogStream,
    ServiceLogStreamEnd,
    ServiceLogStreamLine,
    ServiceLogStreamReady,
)
from heimdall.runtime.models import (
    RuntimeConfigurationError,
    RuntimeDatabase,
    RuntimeDeployment,
    RuntimeService,
)
from heimdall.runtime.process import CommandExecutionError, CommandResult, CommandRunner
from heimdall.runtime.process_stream import (
    CommandLineStream,
    CommandOutputStream,
    CommandStreamEnded,
    CommandStreamRunner,
)
from heimdall.secrets.store import SecretStore, SecretStoreError


class HealthProbe(Protocol):
    def wait_until_healthy(
        self,
        url: str,
        *,
        timeout_seconds: float,
        heartbeat: Callable[[], None],
    ) -> None: ...


class HttpHealthProbe:
    def __init__(self, interval_seconds: float = 0.5) -> None:
        self._interval_seconds = interval_seconds

    def wait_until_healthy(
        self,
        url: str,
        *,
        timeout_seconds: float,
        heartbeat: Callable[[], None],
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                request = Request(url, method="GET")
                with urlopen(request, timeout=min(2, timeout_seconds)) as response:
                    if 200 <= response.status < 400:
                        return
            except (HTTPError, URLError, TimeoutError, RemoteDisconnected, ConnectionError):
                pass
            heartbeat()
            time.sleep(self._interval_seconds)
        raise RuntimeFailure("HEALTH_CHECK", "SERVICE_HEALTH_TIMEOUT")


@dataclass(frozen=True, slots=True)
class RunningService:
    name: str
    container_name: str
    image_name: str
    health_port: int


@dataclass(frozen=True, slots=True)
class CandidateGeneration:
    network_name: str
    services: tuple[RunningService, ...]


class _ResourceState(StrEnum):
    EXACT = "EXACT"
    ABSENT = "ABSENT"
    CONFLICT = "CONFLICT"
    UNKNOWN = "UNKNOWN"


class DockerRuntime:
    def __init__(
        self,
        runner: CommandRunner,
        probe: HealthProbe,
        *,
        executable: str = "docker",
        managed_database_container: str = "heimdall-managed-postgres",
        command_timeout_seconds: float = 900,
        health_timeout_seconds: float = 60,
    ) -> None:
        self._runner = runner
        self._probe = probe
        self._executable = executable
        self._managed_database_container = managed_database_container
        self._command_timeout_seconds = command_timeout_seconds
        self._health_timeout_seconds = health_timeout_seconds

    def start_candidate(
        self,
        deployment: Deployment,
        runtime: RuntimeDeployment,
        source_root: Path,
        secret_store: SecretStore,
        progress: RuntimeProgress,
    ) -> CandidateGeneration:
        source_root = source_root.resolve(strict=True)
        self.cleanup_candidate(deployment, runtime)
        for service in runtime.services:
            self._assert_absent("container", _container_name(deployment, service))
            self._assert_absent("image", _image_name(deployment, service))
        self._assert_absent("network", _network_name(deployment))
        progress.stage(
            DeploymentStatus.BUILDING,
            "IMAGES_BUILDING",
            "Building service images from the selected commit",
        )
        for service in runtime.services:
            context, dockerfile = _build_paths(source_root, service)
            self._run(
                [
                    "build",
                    "--label",
                    "heimdall.managed=true",
                    "--label",
                    f"heimdall.project-id={deployment.project_id}",
                    "--label",
                    f"heimdall.deployment-id={deployment.id}",
                    "--file",
                    str(dockerfile),
                    "--tag",
                    _image_name(deployment, service),
                    str(context),
                ],
                progress,
                RuntimeFailure("BUILD", "IMAGE_BUILD_FAILED"),
            )

        network = _network_name(deployment)
        self._run(
            [
                "network",
                "create",
                "--label",
                "heimdall.managed=true",
                "--label",
                f"heimdall.project-id={deployment.project_id}",
                "--label",
                f"heimdall.deployment-id={deployment.id}",
                network,
            ],
            progress,
            RuntimeFailure("DOCKER", "NETWORK_CREATE_FAILED", retryable=True),
        )
        if any(service.project_database_access for service in runtime.services):
            database = runtime.database
            if database is None:
                raise RuntimeFailure("CONFIGURATION", "DATABASE_METADATA_MISSING")
            self._run(
                [
                    "network",
                    "connect",
                    "--alias",
                    database.host,
                    network,
                    self._managed_database_container,
                ],
                progress,
                RuntimeFailure("DOCKER", "DATABASE_NETWORK_CONNECT_FAILED", retryable=True),
            )

        progress.stage(
            DeploymentStatus.STARTING,
            "SERVICES_STARTING",
            "Starting generation service containers",
        )
        for service in runtime.services:
            arguments = self._create_arguments(
                deployment,
                runtime.database,
                service,
                network,
                secret_store,
            )
            self._run(
                arguments,
                progress,
                RuntimeFailure("START", "SERVICE_START_FAILED", retryable=True),
            )

        for service in runtime.services:
            container = _container_name(deployment, service)
            self._run(
                ["start", container],
                progress,
                RuntimeFailure("START", "SERVICE_START_FAILED", retryable=True),
            )

        # A service may exit during the first pass if it resolves another service at startup.
        # Once every network endpoint has started, a second idempotent start lets it recover.
        for service in runtime.services:
            self._run(
                ["start", _container_name(deployment, service)],
                progress,
                RuntimeFailure("START", "SERVICE_START_FAILED", retryable=True),
            )

        running: list[RunningService] = []
        for service in runtime.services:
            container = _container_name(deployment, service)
            port_result = self._run(
                ["port", container, f"{service.internal_port}/tcp"],
                progress,
                RuntimeFailure("START", "HEALTH_PORT_UNAVAILABLE"),
            )
            health_port = _published_port(port_result.stdout)
            running.append(
                RunningService(
                    name=service.name,
                    container_name=container,
                    image_name=_image_name(deployment, service),
                    health_port=health_port,
                )
            )

        progress.stage(
            DeploymentStatus.HEALTH_CHECKING,
            "SERVICES_HEALTH_CHECKING",
            "Waiting for all candidate services to become healthy",
        )
        candidate = CandidateGeneration(network_name=network, services=tuple(running))
        self.verify_candidate(runtime, candidate, progress)
        return candidate

    def verify_candidate(
        self,
        runtime: RuntimeDeployment,
        candidate: CandidateGeneration,
        progress: RuntimeProgress,
    ) -> None:
        for service, item in zip(runtime.services, candidate.services, strict=True):
            self._probe.wait_until_healthy(
                f"http://127.0.0.1:{item.health_port}{service.health_path}",
                timeout_seconds=self._health_timeout_seconds,
                heartbeat=progress.heartbeat,
            )

    def cleanup_candidate(self, deployment: Deployment, runtime: RuntimeDeployment) -> None:
        for service in runtime.services:
            name = _container_name(deployment, service)
            if self._is_managed("container", name, deployment.id):
                self._run_ignored(["rm", "--force", name])
        network = _network_name(deployment)
        if self._is_managed("network", network, deployment.id):
            self._disconnect_managed_database(network)
            self._run_ignored(["network", "rm", network])
        for service in runtime.services:
            image = _image_name(deployment, service)
            if self._is_managed("image", image, deployment.id):
                self._run_ignored(["image", "rm", "--force", image])

    def cleanup_candidate_verified(
        self,
        deployment: Deployment,
        runtime: RuntimeDeployment,
        progress: RuntimeProgress,
    ) -> None:
        resources = [
            *(("container", _container_name(deployment, service)) for service in runtime.services),
            ("network", _network_name(deployment)),
            *(("image", _image_name(deployment, service)) for service in runtime.services),
        ]
        before = {
            resource: self._resource_state(*resource, deployment, heartbeat=progress.heartbeat)
            for resource in resources
        }
        if any(state is _ResourceState.UNKNOWN for state in before.values()):
            raise RuntimeFailure(
                "RECONCILIATION",
                "CANDIDATE_RESOURCE_OBSERVATION_FAILED",
                retryable=True,
                cleanup_candidate=False,
            )
        if any(state is _ResourceState.CONFLICT for state in before.values()):
            raise RuntimeFailure(
                "RECONCILIATION",
                "CANDIDATE_RESOURCE_NAME_CONFLICT",
                cleanup_candidate=False,
            )

        for kind, name in resources:
            if before[(kind, name)] is not _ResourceState.EXACT:
                continue
            if kind == "network":
                self._run_ignored(
                    [
                        "network",
                        "disconnect",
                        "--force",
                        name,
                        self._managed_database_container,
                    ],
                    heartbeat=progress.heartbeat,
                )
                self._run_ignored(["network", "rm", name], heartbeat=progress.heartbeat)
            elif kind == "image":
                self._run_ignored(["image", "rm", "--force", name], heartbeat=progress.heartbeat)
            else:
                self._run_ignored(["rm", "--force", name], heartbeat=progress.heartbeat)

        after = {
            resource: self._resource_state(*resource, deployment, heartbeat=progress.heartbeat)
            for resource in resources
        }
        if any(state is _ResourceState.UNKNOWN for state in after.values()):
            raise RuntimeFailure(
                "RECONCILIATION",
                "CANDIDATE_RESOURCE_OBSERVATION_FAILED",
                retryable=True,
                cleanup_candidate=False,
            )
        if any(state is _ResourceState.CONFLICT for state in after.values()):
            raise RuntimeFailure(
                "RECONCILIATION",
                "CANDIDATE_RESOURCE_NAME_CONFLICT",
                cleanup_candidate=False,
            )
        if any(state is _ResourceState.EXACT for state in after.values()):
            raise RuntimeFailure(
                "RECONCILIATION",
                "CANDIDATE_CLEANUP_INCOMPLETE",
                retryable=True,
                cleanup_candidate=False,
            )

    def observe_candidate(
        self,
        deployment: Deployment,
        runtime: RuntimeDeployment,
        progress: RuntimeProgress,
    ) -> CandidateGeneration | None:
        network = _network_name(deployment)
        heartbeat = progress.heartbeat
        if not self._is_managed("network", network, deployment.id, heartbeat=heartbeat):
            return None
        services: list[RunningService] = []
        for service in runtime.services:
            container = _container_name(deployment, service)
            image = _image_name(deployment, service)
            if not self._is_managed("container", container, deployment.id, heartbeat=heartbeat):
                return None
            if not self._is_managed("image", image, deployment.id, heartbeat=heartbeat):
                return None
            running = self._run_ignored(
                ["inspect", "--format", "{{json .State.Running}}", container],
                heartbeat=heartbeat,
            )
            if running.returncode != 0:
                return None
            try:
                is_running = json.loads(running.stdout)
            except (json.JSONDecodeError, TypeError):
                return None
            if is_running is not True:
                return None
            published = self._run_ignored(
                ["port", container, f"{service.internal_port}/tcp"],
                heartbeat=heartbeat,
            )
            if published.returncode != 0:
                return None
            try:
                health_port = _published_port(published.stdout)
            except RuntimeFailure:
                return None
            services.append(
                RunningService(
                    name=service.name,
                    container_name=container,
                    image_name=image,
                    health_port=health_port,
                )
            )
        return CandidateGeneration(network_name=network, services=tuple(services))

    def promote_candidate(self, candidate: CandidateGeneration) -> None:
        for service in candidate.services:
            self._run_ignored(["update", "--restart", "unless-stopped", service.container_name])

    def retire_generation(
        self,
        network_name: str,
        container_names: tuple[str, ...],
        image_names: tuple[str, ...],
        deployment_id: UUID,
    ) -> None:
        for container_name in container_names:
            if self._is_managed("container", container_name, deployment_id):
                self._run_ignored(["rm", "--force", container_name])
        if self._is_managed("network", network_name, deployment_id):
            self._disconnect_managed_database(network_name)
            self._run_ignored(["network", "rm", network_name])
        for image_name in image_names:
            if self._is_managed("image", image_name, deployment_id):
                self._run_ignored(["image", "rm", "--force", image_name])

    def _is_managed(
        self,
        kind: str,
        name: str,
        deployment_id: UUID,
        *,
        heartbeat: Callable[[], None] | None = None,
    ) -> bool:
        result = self._inspect(kind, name, heartbeat=heartbeat)
        if result.returncode != 0:
            return False
        try:
            labels = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            return False
        return (
            isinstance(labels, dict)
            and labels.get("heimdall.managed") == "true"
            and labels.get("heimdall.deployment-id") == str(deployment_id)
        )

    def _resource_state(
        self,
        kind: str,
        name: str,
        deployment: Deployment,
        *,
        heartbeat: Callable[[], None] | None = None,
    ) -> _ResourceState:
        result = self._inspect(kind, name, heartbeat=heartbeat)
        if result.returncode == -1:
            return _ResourceState.UNKNOWN
        if result.returncode != 0:
            return _ResourceState.ABSENT
        try:
            labels = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            return _ResourceState.CONFLICT
        if (
            isinstance(labels, dict)
            and labels.get("heimdall.managed") == "true"
            and labels.get("heimdall.project-id") == str(deployment.project_id)
            and labels.get("heimdall.deployment-id") == str(deployment.id)
        ):
            return _ResourceState.EXACT
        return _ResourceState.CONFLICT

    def _assert_absent(self, kind: str, name: str) -> None:
        if self._inspect(kind, name).returncode == 0:
            raise RuntimeFailure("DOCKER", "RESOURCE_NAME_CONFLICT")

    def _disconnect_managed_database(self, network_name: str) -> None:
        self._run_ignored(
            [
                "network",
                "disconnect",
                "--force",
                network_name,
                self._managed_database_container,
            ]
        )

    def _inspect(self, kind: str, name: str, *, heartbeat: Callable[[], None] | None = None):
        if kind == "network":
            arguments = ["network", "inspect", "--format", "{{json .Labels}}", name]
        elif kind == "image":
            arguments = ["image", "inspect", "--format", "{{json .Config.Labels}}", name]
        else:
            arguments = ["inspect", "--format", "{{json .Config.Labels}}", name]
        return self._run_ignored(arguments, heartbeat=heartbeat)

    def _create_arguments(
        self,
        deployment: Deployment,
        database: RuntimeDatabase | None,
        service: RuntimeService,
        network: str,
        secret_store: SecretStore,
    ) -> list[str]:
        arguments = [
            "create",
            "--name",
            _container_name(deployment, service),
            "--network",
            network,
            "--network-alias",
            service.name,
            "--network-alias",
            _gateway_alias(deployment, service),
            "--publish",
            f"127.0.0.1::{service.internal_port}",
            "--label",
            "heimdall.managed=true",
            "--label",
            f"heimdall.project-id={deployment.project_id}",
            "--label",
            f"heimdall.deployment-id={deployment.id}",
            "--env",
            f"HEIMDALL_PROJECT_ID={deployment.project_id}",
            "--env",
            f"HEIMDALL_DEPLOYMENT_ID={deployment.id}",
        ]
        for variable in service.environment:
            arguments.extend(["--env", f"{variable.name}={variable.value}"])
        try:
            for secret in service.secrets:
                source = secret_store.resolve(secret.reference, secret.fingerprint)
                arguments.extend(["--env", f"{secret.name}={secret.container_path}"])
                arguments.extend(["--mount", _bind_mount(source, secret.container_path)])
            if service.project_database_access:
                if database is None:
                    raise RuntimeFailure("CONFIGURATION", "DATABASE_METADATA_MISSING")
                credential = secret_store.resolve(
                    database.credential_reference,
                    database.credential_fingerprint,
                )
                managed_values = {
                    "DATABASE_HOST": database.host,
                    "DATABASE_PORT": str(database.port),
                    "DATABASE_NAME": database.database_name,
                    "DATABASE_USER": database.username,
                    "DATABASE_SCHEMA": database.schema_name,
                    "DATABASE_PASSWORD_FILE": database.container_path,
                }
                for name, value in managed_values.items():
                    arguments.extend(["--env", f"{name}={value}"])
                arguments.extend(["--mount", _bind_mount(credential, database.container_path)])
        except SecretStoreError as error:
            raise RuntimeFailure("SECRET", "SECRET_RESOLUTION_FAILED") from error
        arguments.append(_image_name(deployment, service))
        return arguments

    def _run(
        self,
        arguments: list[str],
        progress: RuntimeProgress,
        failure: RuntimeFailure | None = None,
    ):
        try:
            return self._runner.run(
                [self._executable, *arguments],
                timeout_seconds=self._command_timeout_seconds,
                heartbeat=progress.heartbeat,
            )
        except CommandExecutionError as error:
            resolved = failure or RuntimeFailure("DOCKER", "DOCKER_COMMAND_FAILED", retryable=True)
            raise resolved from error

    def _run_ignored(self, arguments: list[str], *, heartbeat: Callable[[], None] | None = None):
        try:
            return self._runner.run(
                [self._executable, *arguments],
                timeout_seconds=self._command_timeout_seconds,
                heartbeat=heartbeat,
                check=False,
            )
        except CommandExecutionError:
            return CommandResult(-1, "")


@dataclass(frozen=True, slots=True)
class _ServiceLogTarget:
    services: tuple[str, ...]
    service: RuntimeService
    redactions: tuple[str, ...]
    container_id: str


class _DockerServiceLogAccess:
    def __init__(
        self,
        runner: CommandRunner,
        secret_store: SecretStore,
        *,
        executable: str = "docker",
        command_timeout_seconds: float = 5,
    ) -> None:
        if command_timeout_seconds <= 0:
            raise ValueError("service log command timeout must be positive")
        self._runner = runner
        self._secret_store = secret_store
        self._executable = executable
        self._command_timeout_seconds = command_timeout_seconds

    def _resolve(self, deployment: Deployment, service_name: str | None) -> _ServiceLogTarget:
        try:
            runtime = RuntimeDeployment.from_deployment(deployment)
        except RuntimeConfigurationError as error:
            raise ServiceLogError("SERVICE_LOGS_UNAVAILABLE") from error

        services = tuple(service.name for service in runtime.services)
        selected_name = service_name or next(
            route.service for route in runtime.routes if route.path == "/"
        )
        selected = next(
            (service for service in runtime.services if service.name == selected_name),
            None,
        )
        if selected is None:
            raise ServiceLogError("SERVICE_LOG_SERVICE_NOT_FOUND")

        redactions = self._redactions(runtime.database, selected)
        container = _container_name(deployment, selected)
        inspection = self._run(
            [
                "inspect",
                "--format",
                "{{json .Id}} {{json .Config.Labels}}",
                container,
            ]
        )
        container_id = (
            _exact_container_id(inspection.stdout, deployment)
            if inspection.returncode == 0
            else None
        )
        if container_id is None:
            raise ServiceLogError("SERVICE_LOGS_UNAVAILABLE")

        return _ServiceLogTarget(services, selected, redactions, container_id)

    def _redactions(
        self,
        database: RuntimeDatabase | None,
        service: RuntimeService,
    ) -> tuple[str, ...]:
        values: list[str] = []
        try:
            for secret in service.secrets:
                values.append(self._secret_store.read(secret.reference, secret.fingerprint))
            if service.project_database_access:
                if database is None:
                    raise SecretStoreError("database metadata is missing")
                values.append(
                    self._secret_store.read(
                        database.credential_reference,
                        database.credential_fingerprint,
                    )
                )
        except (KeyError, OSError, UnicodeError, SecretStoreError) as error:
            raise ServiceLogError("SERVICE_LOG_REDACTION_UNAVAILABLE") from error
        if any(
            not value
            or "\n" in value
            or "\r" in value
            or len(value.encode("utf-8")) > SERVICE_LOG_MAX_LINE_BYTES
            for value in values
        ):
            raise ServiceLogError("SERVICE_LOG_REDACTION_UNAVAILABLE")
        return tuple(sorted(set(values), key=len, reverse=True))

    def _run(self, arguments: list[str]) -> CommandResult:
        try:
            return self._runner.run(
                [self._executable, *arguments],
                timeout_seconds=self._command_timeout_seconds,
                check=False,
            )
        except (CommandExecutionError, subprocess.TimeoutExpired) as error:
            raise ServiceLogError("SERVICE_LOGS_UNAVAILABLE") from error


class DockerServiceLogReader(_DockerServiceLogAccess):
    def read(self, deployment: Deployment, service_name: str | None) -> ServiceLogSnapshot:
        target = self._resolve(deployment, service_name)

        result = self._run(
            ["logs", "--tail", str(SERVICE_LOG_TAIL), "--timestamps", target.container_id]
        )
        if result.returncode != 0:
            raise ServiceLogError("SERVICE_LOGS_UNAVAILABLE")

        lines, truncated = _service_log_lines(result, target.redactions)
        return ServiceLogSnapshot(
            deployment_id=deployment.id,
            services=target.services,
            service_name=target.service.name,
            retrieved_at=datetime.now(UTC),
            lines=lines,
            truncated=truncated,
        )


class DockerServiceLogStreamer(_DockerServiceLogAccess):
    def __init__(
        self,
        runner: CommandRunner,
        stream_runner: CommandStreamRunner,
        secret_store: SecretStore,
        *,
        executable: str = "docker",
        command_timeout_seconds: float = 5,
    ) -> None:
        super().__init__(
            runner,
            secret_store,
            executable=executable,
            command_timeout_seconds=command_timeout_seconds,
        )
        self._stream_runner = stream_runner

    def open(
        self,
        deployment: Deployment,
        service_name: str | None,
    ) -> DockerServiceLogSubscription:
        target = self._resolve(deployment, service_name)
        redaction_bytes = tuple(value.encode("utf-8") for value in target.redactions)
        raw_line_limit = (
            SERVICE_LOG_MAX_LINE_BYTES
            + max((len(value) for value in redaction_bytes), default=0)
            + 128
        )
        try:
            stream = self._stream_runner.open(
                [
                    self._executable,
                    "logs",
                    "--tail",
                    str(SERVICE_LOG_TAIL),
                    "--follow",
                    "--timestamps",
                    target.container_id,
                ],
                max_line_bytes=raw_line_limit,
            )
        except (CommandExecutionError, OSError, ValueError) as error:
            raise ServiceLogError("SERVICE_LOGS_UNAVAILABLE") from error
        return DockerServiceLogSubscription(
            ServiceLogStreamReady(
                deployment_id=deployment.id,
                services=target.services,
                service_name=target.service.name,
                connected_at=datetime.now(UTC),
            ),
            stream,
            redaction_bytes,
        )


class DockerServiceLogSubscription:
    def __init__(
        self,
        ready: ServiceLogStreamReady,
        stream: CommandLineStream,
        redactions: tuple[bytes, ...],
    ) -> None:
        self.ready = ready
        self._stream = stream
        self._redactions = redactions

    def receive(self, timeout_seconds: float) -> ServiceLogStreamLine | ServiceLogStreamEnd | None:
        while True:
            try:
                output = self._stream.receive(timeout_seconds)
            except CommandStreamEnded as ended:
                if ended.returncode != 0:
                    raise ServiceLogError("SERVICE_LOGS_UNAVAILABLE") from ended
                return ServiceLogStreamEnd()
            if output is None:
                return None
            timestamp, separator, message = output.payload.partition(b" ")
            try:
                decoded_timestamp = timestamp.decode("ascii")
            except UnicodeDecodeError:
                continue
            if not separator or _DOCKER_TIMESTAMP.fullmatch(decoded_timestamp) is None:
                continue
            for value in self._redactions:
                message = message.replace(value, b"[REDACTED]")
            bounded = message[:SERVICE_LOG_MAX_LINE_BYTES]
            decoded_message, decode_truncated = _bounded_utf8(
                bounded.decode("utf-8", errors="replace"),
                SERVICE_LOG_MAX_LINE_BYTES,
            )
            return ServiceLogStreamLine(
                line=ServiceLogLine(
                    timestamp=decoded_timestamp,
                    stream=(
                        ServiceLogStream.STDOUT
                        if output.stream is CommandOutputStream.STDOUT
                        else ServiceLogStream.STDERR
                    ),
                    message=decoded_message,
                ),
                truncated=(
                    output.truncated
                    or len(message) > SERVICE_LOG_MAX_LINE_BYTES
                    or decode_truncated
                ),
            )

    def close(self) -> None:
        self._stream.close()


_DOCKER_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$"
)


def _exact_container_id(payload: str, deployment: Deployment) -> str | None:
    raw_id, separator, raw_labels = payload.partition(" ")
    if not separator:
        return None
    try:
        container_id = json.loads(raw_id)
        labels = json.loads(raw_labels)
    except (json.JSONDecodeError, TypeError):
        return None
    if (
        isinstance(container_id, str)
        and re.fullmatch(r"[0-9a-f]{64}", container_id) is not None
        and isinstance(labels, dict)
        and labels.get("heimdall.managed") == "true"
        and labels.get("heimdall.project-id") == str(deployment.project_id)
        and labels.get("heimdall.deployment-id") == str(deployment.id)
    ):
        return container_id
    return None


def _service_log_lines(
    result: CommandResult,
    redactions: tuple[str, ...],
) -> tuple[tuple[ServiceLogLine, ...], bool]:
    entries: list[tuple[str, int, ServiceLogLine]] = []
    truncated = result.stdout_truncated or result.stderr_truncated
    sequence = 0
    for stream, payload in (
        (ServiceLogStream.STDOUT, result.stdout),
        (ServiceLogStream.STDERR, result.stderr),
    ):
        raw_lines = payload.splitlines()
        for raw_line in raw_lines:
            timestamp, separator, message = raw_line.partition(" ")
            if not separator or _DOCKER_TIMESTAMP.fullmatch(timestamp) is None:
                truncated = True
                continue
            for value in redactions:
                message = message.replace(value, "[REDACTED]")
            message, line_truncated = _bounded_utf8(message, SERVICE_LOG_MAX_LINE_BYTES)
            truncated = truncated or line_truncated
            entries.append(
                (
                    timestamp,
                    sequence,
                    ServiceLogLine(timestamp=timestamp, stream=stream, message=message),
                )
            )
            sequence += 1

    entries.sort(key=lambda item: (item[0], item[1]))
    if len(entries) > SERVICE_LOG_TAIL:
        entries = entries[-SERVICE_LOG_TAIL:]
        truncated = True
    return tuple(item[2] for item in entries), truncated


def _bounded_utf8(value: str, limit: int) -> tuple[str, bool]:
    payload = value.encode("utf-8")
    if len(payload) <= limit:
        return value, False
    return payload[:limit].decode("utf-8", errors="ignore"), True


def _build_paths(source_root: Path, service: RuntimeService) -> tuple[Path, Path]:
    context = source_root.joinpath(*service.build_context.parts).resolve()
    dockerfile = context.joinpath(*service.dockerfile.parts).resolve()
    if not context.is_relative_to(source_root) or not context.is_dir():
        raise RuntimeFailure("SOURCE", "BUILD_CONTEXT_INVALID")
    if not dockerfile.is_relative_to(context) or not dockerfile.is_file():
        raise RuntimeFailure("SOURCE", "DOCKERFILE_INVALID")
    return context, dockerfile


def _network_name(deployment: Deployment) -> str:
    return f"hm-p{deployment.project_id.hex[:12]}-g{deployment.id.hex[:12]}"


def _container_name(deployment: Deployment, service: RuntimeService) -> str:
    return f"hm-p{deployment.project_id.hex[:12]}-{service.name}-g{deployment.id.hex[:12]}"


def _image_name(deployment: Deployment, service: RuntimeService) -> str:
    return f"heimdall/{deployment.project_id.hex}:g{deployment.id.hex[:12]}-{service.name}"


def _gateway_alias(deployment: Deployment, service: RuntimeService) -> str:
    return f"{service.name}-g-{deployment.id.hex[:12]}"


def _bind_mount(source: Path, destination: str) -> str:
    if not source.is_absolute() or not source.is_file():
        raise RuntimeFailure("SECRET", "SECRET_FILE_INVALID")
    if "," in str(source):
        raise RuntimeFailure("SECRET", "SECRET_PATH_INVALID")
    return f"type=bind,src={source},dst={destination},readonly"


def _published_port(output: str) -> int:
    candidates = [line.strip() for line in output.splitlines() if line.strip()]
    for value in candidates:
        _, separator, raw_port = value.rpartition(":")
        if separator and raw_port.isdigit() and 1 <= int(raw_port) <= 65535:
            return int(raw_port)
    raise RuntimeFailure("START", "HEALTH_PORT_UNAVAILABLE")
