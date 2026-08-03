from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class ProjectStatus(StrEnum):
    DRAFT = "DRAFT"
    READY = "READY"


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


class ProjectNotFoundError(LookupError):
    pass


class ProjectConflictError(RuntimeError):
    pass


class ProjectVersionConflictError(RuntimeError):
    pass
