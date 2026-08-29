from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class ProjectDatabaseStatus(StrEnum):
    PROVISIONING = "PROVISIONING"
    ACTIVE = "ACTIVE"
    FAILED = "FAILED"


class ProjectDatabasePhase(StrEnum):
    INTENT_RECORDED = "INTENT_RECORDED"
    SECRET_READY = "SECRET_READY"
    ROLE_READY = "ROLE_READY"
    DATABASE_READY = "DATABASE_READY"
    PRIVILEGES_READY = "PRIVILEGES_READY"
    ACTIVE = "ACTIVE"


@dataclass(frozen=True, slots=True)
class ProjectDatabaseResource:
    id: UUID
    project_id: UUID
    desired_state: str
    status: ProjectDatabaseStatus
    phase: ProjectDatabasePhase
    database_name: str
    role_name: str
    schema_name: str
    credential_reference: str | None
    credential_version: int | None
    credential_fingerprint: str | None
    state_version: int
    failure_stage: str | None
    failure_code: str | None
    created_at: datetime
    updated_at: datetime


class ProjectDatabaseVersionConflict(RuntimeError):
    pass


class ProjectDatabaseProjectDeletingError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("PROJECT_DELETING")


@dataclass(slots=True)
class ProjectDatabaseProvisioningError(Exception):
    stage: str
    code: str
