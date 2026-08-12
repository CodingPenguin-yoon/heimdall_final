from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

from heimdall.deployments.diagnostics import (
    DIAGNOSTIC_ARTIFACT_MAX_BYTES,
    DiagnosticArtifactDraft,
    DiagnosticArtifactKind,
    DiagnosticCaptureStatus,
    DiagnosticLine,
    DiagnosticStream,
)
from heimdall.deployments.models import Deployment
from heimdall.deployments.worker import RuntimeFailure
from heimdall.runtime.docker_logs import DockerServiceLogReader, deployment_log_redactions
from heimdall.runtime.logs import SERVICE_LOG_MAX_LINE_BYTES, ServiceLogError
from heimdall.runtime.models import RuntimeConfigurationError, RuntimeDeployment
from heimdall.secrets.store import SecretStore


class DockerDeploymentDiagnosticCollector:
    def __init__(
        self,
        service_logs: DockerServiceLogReader,
        secret_store: SecretStore,
    ) -> None:
        self._service_logs = service_logs
        self._secret_store = secret_store

    def capture(
        self,
        deployment: Deployment,
        failure: RuntimeFailure,
        heartbeat: Callable[[], None] | None = None,
    ) -> tuple[DiagnosticArtifactDraft, ...]:
        artifacts: list[DiagnosticArtifactDraft] = []
        if failure.command_output is not None:
            artifacts.append(self._command_artifact(deployment, failure))
            if heartbeat is not None:
                heartbeat()
        try:
            runtime = RuntimeDeployment.from_deployment(deployment)
        except RuntimeConfigurationError:
            return tuple(artifacts)
        for service in runtime.services:
            artifacts.append(self._service_artifact(deployment, service.name))
            if heartbeat is not None:
                heartbeat()
        return tuple(artifacts)

    def _command_artifact(
        self,
        deployment: Deployment,
        failure: RuntimeFailure,
    ) -> DiagnosticArtifactDraft:
        command = failure.command_output
        assert command is not None
        captured_at = datetime.now(UTC)
        try:
            redactions = deployment_log_redactions(deployment, self._secret_store)
        except ServiceLogError as error:
            return DiagnosticArtifactDraft(
                kind=DiagnosticArtifactKind.COMMAND_OUTPUT,
                capture_status=DiagnosticCaptureStatus.UNAVAILABLE,
                capture_code=error.code,
                operation=command.operation,
                service_name=command.service_name,
                return_code=command.return_code,
                container_status=None,
                container_exit_code=None,
                lines=(),
                truncated=command.stdout_truncated or command.stderr_truncated,
                captured_at=captured_at,
            )
        lines: list[DiagnosticLine] = []
        for stream, payload in (
            (DiagnosticStream.STDOUT, command.stdout),
            (DiagnosticStream.STDERR, command.stderr),
        ):
            for raw_line in payload.splitlines():
                for value in redactions:
                    raw_line = raw_line.replace(value, "[REDACTED]")
                message, line_truncated = _bounded_utf8(raw_line, SERVICE_LOG_MAX_LINE_BYTES)
                lines.append(DiagnosticLine(stream=stream, message=message))
                if line_truncated:
                    lines.append(DiagnosticLine(stream=stream, message="[line output truncated]"))
        bounded, payload_truncated = _bounded_lines(tuple(lines))
        return DiagnosticArtifactDraft(
            kind=DiagnosticArtifactKind.COMMAND_OUTPUT,
            capture_status=DiagnosticCaptureStatus.CAPTURED,
            capture_code=None,
            operation=command.operation,
            service_name=command.service_name,
            return_code=command.return_code,
            container_status=None,
            container_exit_code=None,
            lines=bounded,
            truncated=(command.stdout_truncated or command.stderr_truncated or payload_truncated),
            captured_at=captured_at,
        )

    def _service_artifact(
        self,
        deployment: Deployment,
        service_name: str,
    ) -> DiagnosticArtifactDraft:
        captured_at = datetime.now(UTC)
        try:
            snapshot, container_status, container_exit_code = self._service_logs.read_diagnostic(
                deployment, service_name
            )
        except ServiceLogError as error:
            return DiagnosticArtifactDraft(
                kind=DiagnosticArtifactKind.SERVICE_LOG,
                capture_status=DiagnosticCaptureStatus.UNAVAILABLE,
                capture_code=error.code,
                operation=None,
                service_name=service_name,
                return_code=None,
                container_status=None,
                container_exit_code=None,
                lines=(),
                truncated=False,
                captured_at=captured_at,
            )
        lines = tuple(
            DiagnosticLine(
                timestamp=line.timestamp,
                stream=DiagnosticStream(line.stream.value),
                message=line.message,
            )
            for line in snapshot.lines
        )
        bounded, payload_truncated = _bounded_lines(lines)
        return DiagnosticArtifactDraft(
            kind=DiagnosticArtifactKind.SERVICE_LOG,
            capture_status=DiagnosticCaptureStatus.CAPTURED,
            capture_code=None,
            operation=None,
            service_name=service_name,
            return_code=None,
            container_status=container_status,
            container_exit_code=container_exit_code,
            lines=bounded,
            truncated=snapshot.truncated or payload_truncated,
            captured_at=snapshot.retrieved_at,
        )


def _bounded_lines(lines: tuple[DiagnosticLine, ...]) -> tuple[tuple[DiagnosticLine, ...], bool]:
    selected = list(lines)
    truncated = False
    while selected and _encoded_size(selected) > DIAGNOSTIC_ARTIFACT_MAX_BYTES:
        selected.pop(0)
        truncated = True
    if _encoded_size(selected) > DIAGNOSTIC_ARTIFACT_MAX_BYTES:
        return (), True
    return tuple(selected), truncated


def _encoded_size(lines: list[DiagnosticLine]) -> int:
    return len(
        json.dumps(
            [
                {
                    "timestamp": line.timestamp,
                    "stream": line.stream.value,
                    "message": line.message,
                }
                for line in lines
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _bounded_utf8(value: str, limit: int) -> tuple[str, bool]:
    payload = value.encode("utf-8")
    if len(payload) <= limit:
        return value, False
    return payload[:limit].decode("utf-8", errors="ignore"), True
