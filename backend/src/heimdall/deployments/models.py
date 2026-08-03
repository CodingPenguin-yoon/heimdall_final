from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class DeploymentSource(StrEnum):
    MAIN_HEAD = "MAIN_HEAD"
    MAIN_COMMIT = "MAIN_COMMIT"


class DeploymentStatus(StrEnum):
    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    BUILDING = "BUILDING"
    STARTING = "STARTING"
    HEALTH_CHECKING = "HEALTH_CHECKING"
    ACTIVATING = "ACTIVATING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class Deployment:
    id: UUID
    project_id: UUID
    source_type: DeploymentSource
    requested_commit_sha: str | None
    resolved_commit_sha: str
    config_version: int
    config_snapshot: dict[str, Any]
    status: DeploymentStatus
    failure_stage: str | None
    failure_code: str | None
    created_at: datetime
    updated_at: datetime
    terminal_at: datetime | None


class DeploymentNotFoundError(LookupError):
    pass


class ActiveDeploymentError(RuntimeError):
    pass
