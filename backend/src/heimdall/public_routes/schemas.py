from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import Field, field_validator

from heimdall.common.api_model import ApiModel
from heimdall.public_routes.models import (
    PublicRoute,
    PublicRouteDesiredState,
    PublicRouteStatus,
)

PUBLIC_SUBDOMAIN_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class PublicRouteUpdate(ApiModel):
    subdomain: Annotated[str, Field(min_length=1, max_length=63)]

    @field_validator("subdomain", mode="before")
    @classmethod
    def normalize_subdomain(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("subdomain")
    @classmethod
    def validate_subdomain(cls, value: str) -> str:
        if not value.isascii() or PUBLIC_SUBDOMAIN_PATTERN.fullmatch(value) is None:
            raise ValueError("must be a lowercase ASCII DNS label")
        if "--" in value:
            raise ValueError("must not contain consecutive separators")
        return value


class PublicRouteRead(ApiModel):
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

    @classmethod
    def from_route(cls, route: PublicRoute) -> PublicRouteRead:
        return cls(
            project_id=route.project_id,
            subdomain=route.subdomain,
            hostname=route.hostname,
            desired_state=route.desired_state,
            status=route.status,
            desired_revision=route.desired_revision,
            applied_revision=route.applied_revision,
            applied_hostname=route.applied_hostname,
            last_error_code=route.last_error_code,
            created_at=route.created_at,
            updated_at=route.updated_at,
        )
