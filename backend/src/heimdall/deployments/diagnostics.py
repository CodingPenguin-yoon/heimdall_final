from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

DIAGNOSTIC_ARTIFACT_MAX_BYTES = 262_144


class DiagnosticArtifactKind(StrEnum):
    COMMAND_OUTPUT = "COMMAND_OUTPUT"
    SERVICE_LOG = "SERVICE_LOG"


class DiagnosticCaptureStatus(StrEnum):
    CAPTURED = "CAPTURED"
    UNAVAILABLE = "UNAVAILABLE"


class DiagnosticStream(StrEnum):
    STDOUT = "STDOUT"
    STDERR = "STDERR"


@dataclass(frozen=True, slots=True)
class DiagnosticLine:
    stream: DiagnosticStream
    message: str
    timestamp: str | None = None


@dataclass(frozen=True, slots=True)
class DiagnosticArtifactDraft:
    kind: DiagnosticArtifactKind
    capture_status: DiagnosticCaptureStatus
    capture_code: str | None
    operation: str | None
    service_name: str | None
    return_code: int | None
    container_status: str | None
    container_exit_code: int | None
    lines: tuple[DiagnosticLine, ...]
    truncated: bool
    captured_at: datetime


@dataclass(frozen=True, slots=True)
class DeploymentDiagnosticArtifact:
    id: UUID
    deployment_id: UUID
    event_id: int
    kind: DiagnosticArtifactKind
    failure_stage: str
    failure_code: str
    capture_status: DiagnosticCaptureStatus
    capture_code: str | None
    operation: str | None
    service_name: str | None
    return_code: int | None
    container_status: str | None
    container_exit_code: int | None
    line_count: int
    byte_count: int
    truncated: bool
    captured_at: datetime
    expires_at: datetime
    lines: tuple[DiagnosticLine, ...] | None


@dataclass(frozen=True, slots=True)
class FailedCommandOutput:
    operation: str
    return_code: int
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    service_name: str | None = None


class DeploymentDiagnosticNotFoundError(LookupError):
    pass
