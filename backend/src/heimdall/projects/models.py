from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class ProjectStatus(StrEnum):
    DRAFT = "DRAFT"
    READY = "READY"
    DELETING = "DELETING"


class ProjectDeletionState(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    FAILED = "FAILED"


class ProjectDeletionPhase(StrEnum):
    REQUESTED = "REQUESTED"
    WAITING_FOR_OPERATIONS = "WAITING_FOR_OPERATIONS"
    ROUTE_DISABLING = "ROUTE_DISABLING"
    ROUTE_REMOVED = "ROUTE_REMOVED"
    RUNTIME_CLEANUP = "RUNTIME_CLEANUP"
    DATABASE_QUIESCING = "DATABASE_QUIESCING"
    DATABASE_DROP_DATABASE = "DATABASE_DROP_DATABASE"
    DATABASE_DROP_ROLE = "DATABASE_DROP_ROLE"
    SECRET_CLEANUP = "SECRET_CLEANUP"
    METADATA_DELETE = "METADATA_DELETE"


@dataclass(frozen=True, slots=True)
class Project:
    id: UUID
    name: str
    repository_url: str
    branch: str
    status: ProjectStatus
    config_version: int
    deployment_config: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime
    has_managed_database: bool = False


@dataclass(frozen=True, slots=True)
class ProjectEnvironmentSecret:
    project_id: UUID
    service_name: str
    variable_name: str
    secret_reference: str
    secret_version: int
    secret_fingerprint: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProjectDeletionJob:
    project_id: UUID
    state: ProjectDeletionState
    phase: ProjectDeletionPhase
    attempts: int
    available_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    claim_token: UUID | None
    last_error_code: str | None
    last_error_retryable: bool | None
    delete_managed_database: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProjectDeletionJobClaim:
    job: ProjectDeletionJob
    token: UUID
    worker_id: str
    lease_expires_at: datetime


class ProjectNotFoundError(LookupError):
    pass


class ProjectConflictError(RuntimeError):
    pass


class ProjectVersionConflictError(RuntimeError):
    pass


class ProjectDeletionValidationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ProjectDeletionConflictError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ProjectDeletionNotFoundError(LookupError):
    pass


class ProjectDeletionClaimLostError(RuntimeError):
    pass
