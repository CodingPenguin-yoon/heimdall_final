from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class PublicRouteDesiredState(StrEnum):
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"


class PublicRouteStatus(StrEnum):
    PENDING = "PENDING"
    APPLYING = "APPLYING"
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"


class PublicRouteJobState(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class PublicRoute:
    project_id: UUID
    subdomain: str
    hostname: str
    desired_state: PublicRouteDesiredState
    status: PublicRouteStatus
    desired_revision: int
    applied_revision: int | None
    applied_hostname: str | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PublicRouteJob:
    project_id: UUID
    desired_revision: int
    state: PublicRouteJobState
    attempts: int
    available_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    claim_token: UUID | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class PublicRouteJobClaim:
    route: PublicRoute
    token: UUID
    worker_id: str
    desired_revision: int
    attempts: int
    lease_expires_at: datetime


class PublicRouteNotFoundError(LookupError):
    pass


class PublicRouteConflictError(RuntimeError):
    pass


class PublicRouteProjectDeletingError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("PROJECT_DELETING")


class PublicRouteClaimLostError(RuntimeError):
    pass


class PublicRouteSnapshotChangedError(RuntimeError):
    pass
