from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime

from heimdall.deployments.models import Deployment
from heimdall.runtime.docker import container_name
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

        redactions = deployment_log_redactions(deployment, self._secret_store)
        container = container_name(deployment, selected)
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

    def _run(self, arguments: list[str]) -> CommandResult:
        try:
            return self._runner.run(
                [self._executable, *arguments],
                timeout_seconds=self._command_timeout_seconds,
                check=False,
            )
        except (CommandExecutionError, subprocess.TimeoutExpired) as error:
            raise ServiceLogError("SERVICE_LOGS_UNAVAILABLE") from error


def deployment_log_redactions(
    deployment: Deployment,
    secret_store: SecretStore,
) -> tuple[str, ...]:
    try:
        runtime = RuntimeDeployment.from_deployment(deployment)
    except RuntimeConfigurationError as error:
        raise ServiceLogError("SERVICE_LOG_REDACTION_UNAVAILABLE") from error
    values: list[str] = []
    try:
        for service in runtime.services:
            for secret in service.secrets:
                values.append(secret_store.read(secret.reference, secret.fingerprint))
        if runtime.database is not None and any(
            service.project_database_access for service in runtime.services
        ):
            values.append(
                secret_store.read(
                    runtime.database.credential_reference,
                    runtime.database.credential_fingerprint,
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


class DockerServiceLogReader(_DockerServiceLogAccess):
    def read(self, deployment: Deployment, service_name: str | None) -> ServiceLogSnapshot:
        target = self._resolve(deployment, service_name)

        return self._read_target(deployment, target)

    def read_diagnostic(
        self,
        deployment: Deployment,
        service_name: str,
    ) -> tuple[ServiceLogSnapshot, str | None, int | None]:
        target = self._resolve(deployment, service_name)
        snapshot = self._read_target(deployment, target)
        try:
            state = self._run(
                [
                    "inspect",
                    "--format",
                    "{{json .State.Status}} {{json .State.ExitCode}}",
                    target.container_id,
                ]
            )
        except ServiceLogError:
            return snapshot, None, None
        if state.returncode != 0:
            return snapshot, None, None
        raw_status, separator, raw_exit_code = state.stdout.strip().partition(" ")
        try:
            status = json.loads(raw_status)
            exit_code = json.loads(raw_exit_code) if separator else None
        except (json.JSONDecodeError, TypeError):
            return snapshot, None, None
        if not isinstance(status, str) or not isinstance(exit_code, int):
            return snapshot, None, None
        return snapshot, status[:32], exit_code

    def _read_target(
        self,
        deployment: Deployment,
        target: _ServiceLogTarget,
    ) -> ServiceLogSnapshot:

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
