from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

SERVICE_LOG_TAIL = 200
SERVICE_LOG_MAX_LINE_BYTES = 16 * 1024
SERVICE_LOG_MAX_RESPONSE_BYTES = 256 * 1024
SERVICE_LOG_STREAM_MAX_FRAME_BYTES = 128 * 1024
SERVICE_LOG_STREAM_HEARTBEAT_SECONDS = 5.0


class ServiceLogStream(StrEnum):
    STDOUT = "STDOUT"
    STDERR = "STDERR"


@dataclass(frozen=True, slots=True)
class ServiceLogLine:
    timestamp: str
    stream: ServiceLogStream
    message: str


@dataclass(frozen=True, slots=True)
class ServiceLogSnapshot:
    deployment_id: UUID
    services: tuple[str, ...]
    service_name: str
    retrieved_at: datetime
    lines: tuple[ServiceLogLine, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class ServiceLogStreamReady:
    deployment_id: UUID
    services: tuple[str, ...]
    service_name: str
    connected_at: datetime


@dataclass(frozen=True, slots=True)
class ServiceLogStreamLine:
    line: ServiceLogLine
    truncated: bool


@dataclass(frozen=True, slots=True)
class ServiceLogStreamEnd:
    reason: str = "CONTAINER_LOG_ENDED"


ServiceLogStreamEvent = ServiceLogStreamLine | ServiceLogStreamEnd


@dataclass(frozen=True, slots=True)
class ServiceLogError(RuntimeError):
    code: str
