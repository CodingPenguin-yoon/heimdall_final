from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from heimdall.common.api_model import ApiModel
from heimdall.project_database.models import ProjectDatabaseResource


class ProjectDatabaseRead(ApiModel):
    required: bool
    status: Literal["NOT_CREATED", "PROVISIONING", "ACTIVE", "FAILED"]
    id: UUID | None = None
    phase: str | None = None
    database_name: str | None = None
    username: str | None = None
    schema_name: str | None = None
    host: str | None = None
    port: int | None = None
    connected_services: list[str] = Field(default_factory=list)
    failure_stage: str | None = None
    failure_code: str | None = None
    updated_at: datetime | None = None

    @classmethod
    def not_created(cls, connected_services: list[str]) -> ProjectDatabaseRead:
        return cls(required=True, status="NOT_CREATED", connected_services=connected_services)

    @classmethod
    def from_resource(
        cls,
        resource: ProjectDatabaseResource,
        host: str,
        port: int,
        connected_services: list[str],
    ) -> ProjectDatabaseRead:
        return cls(
            required=True,
            status=resource.status.value,
            id=resource.id,
            phase=resource.phase.value,
            database_name=resource.database_name,
            username=resource.role_name,
            schema_name=resource.schema_name,
            host=host,
            port=port,
            connected_services=connected_services,
            failure_stage=resource.failure_stage,
            failure_code=resource.failure_code,
            updated_at=resource.updated_at,
        )
