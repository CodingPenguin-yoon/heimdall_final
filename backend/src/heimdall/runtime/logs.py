from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

SERVICE_LOG_TAIL = 200
SERVICE_LOG_MAX_LINE_BYTES = 16 * 1024
SERVICE_LOG_MAX_RESPONSE_BYTES = 256 * 1024


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
class ServiceLogError(RuntimeError):
    code: str
