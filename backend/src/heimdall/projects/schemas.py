from __future__ import annotations

import re
from dataclasses import asdict
from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from heimdall.common.api_model import ApiModel
from heimdall.projects.models import Project, ProjectDeletionJob, ProjectStatus

SERVICE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
ENVIRONMENT_NAME_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
HTTP_PATH_PATTERN = re.compile(r"^/(?:[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*)?$")


def _repository_relative_path(value: str) -> str:
    normalized = value.strip()
    parts = normalized.split("/")
    invalid = (
        not normalized
        or normalized.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
    )
    if normalized != "." and invalid:
        raise ValueError("must be a canonical repository-relative path")
    return normalized


class ProjectCreate(ApiModel):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    repository_url: Annotated[str, Field(min_length=20, max_length=2048)]

    @field_validator("name", "repository_url")
    @classmethod
    def strip_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


class ProjectDeletionRequest(ApiModel):
    confirmation: Annotated[str, Field(min_length=36, max_length=36)]
    delete_managed_database: bool = False
    managed_database_confirmation: Annotated[str, Field(max_length=128)] | None = None


class ProjectDeletionRead(ApiModel):
    project_id: UUID
    state: str
    phase: str
    attempts: int
    available_at: datetime
    last_error_code: str | None
    last_error_retryable: bool | None
    delete_managed_database: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_job(cls, job: ProjectDeletionJob) -> ProjectDeletionRead:
        values = asdict(job)
        for internal in ("lease_owner", "lease_expires_at", "claim_token"):
            values.pop(internal)
        return cls(**values)


class ServiceBuild(ApiModel):
    context: Annotated[str, Field(max_length=512)] = "."
    dockerfile: Annotated[str, Field(max_length=512)] = "Dockerfile"

    @field_validator("context", "dockerfile")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _repository_relative_path(value)


class EnvironmentVariableKind(StrEnum):
    PLAIN = "PLAIN"
    SECRET = "SECRET"


class EnvironmentVariableInput(ApiModel):
    name: Annotated[str, Field(min_length=1, max_length=128)]
    kind: EnvironmentVariableKind = EnvironmentVariableKind.PLAIN
    value: Annotated[str, Field(max_length=8192)] | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not ENVIRONMENT_NAME_PATTERN.fullmatch(value):
            raise ValueError("must be an uppercase environment variable name")
        if value.startswith(("DATABASE_", "HEIMDALL_")):
            raise ValueError("is reserved for Heimdall managed values")
        return value

    @model_validator(mode="after")
    def validate_value(self) -> EnvironmentVariableInput:
        if self.kind is EnvironmentVariableKind.PLAIN and self.value is None:
            raise ValueError("plain environment variable requires a value")
        if self.value is not None and "\x00" in self.value:
            raise ValueError("environment variable must not contain a null byte")
        return self


class ServiceConfig(ApiModel):
    name: Annotated[str, Field(min_length=1, max_length=32)]
    build: ServiceBuild
    internal_port: Annotated[int, Field(ge=1, le=65535)]
    health_path: Annotated[str, Field(min_length=1, max_length=1024)] = "/health"
    environment: list[EnvironmentVariableInput] = Field(default_factory=list, max_length=64)
    project_database_access: bool = False

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not SERVICE_NAME_PATTERN.fullmatch(value):
            raise ValueError("must be a lowercase DNS label")
        return value

    @field_validator("health_path")
    @classmethod
    def validate_health_path(cls, value: str) -> str:
        if HTTP_PATH_PATTERN.fullmatch(value) is None:
            raise ValueError("must be a canonical absolute path")
        return value

    @model_validator(mode="after")
    def validate_environment(self) -> ServiceConfig:
        names = [item.name for item in self.environment]
        if len(names) != len(set(names)):
            raise ValueError("environment variable names must be unique per service")
        return self


class RouteConfig(ApiModel):
    path: Annotated[str, Field(min_length=1, max_length=1024)]
    service: Annotated[str, Field(min_length=1, max_length=32)]

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if HTTP_PATH_PATTERN.fullmatch(value) is None:
            raise ValueError("must be a canonical absolute path")
        if value != "/" and value.endswith("/"):
            raise ValueError("must not end with a slash")
        return value


class DeploymentConfig(ApiModel):
    services: Annotated[list[ServiceConfig], Field(min_length=1, max_length=16)]
    routes: Annotated[list[RouteConfig], Field(min_length=1, max_length=64)]

    @model_validator(mode="after")
    def validate_relationships(self) -> DeploymentConfig:
        service_names = [service.name for service in self.services]
        if len(service_names) != len(set(service_names)):
            raise ValueError("service names must be unique")
        route_paths = [route.path for route in self.routes]
        if len(route_paths) != len(set(route_paths)):
            raise ValueError("route paths must be unique")
        if "/" not in route_paths:
            raise ValueError("one route must own the root path")
        unknown = {route.service for route in self.routes} - set(service_names)
        if unknown:
            raise ValueError(f"routes reference unknown services: {', '.join(sorted(unknown))}")
        return self


class ProjectSettingsUpdate(DeploymentConfig):
    expected_version: Annotated[int, Field(ge=0)]

    def snapshot(self) -> dict:
        services = []
        for service in self.services:
            item = service.model_dump(mode="json", by_alias=True, exclude={"environment"})
            item["environment"] = [
                {
                    "name": variable.name,
                    "kind": variable.kind.value,
                    **(
                        {"value": variable.value}
                        if variable.kind is EnvironmentVariableKind.PLAIN
                        else {"configured": variable.value is not None}
                    ),
                }
                for variable in service.environment
            ]
            services.append(item)
        return {
            "services": services,
            "routes": [route.model_dump(mode="json", by_alias=True) for route in self.routes],
        }


class ProjectRead(ApiModel):
    id: UUID
    name: str
    repository_url: str
    branch: str
    status: ProjectStatus
    config_version: int
    deployment_config: dict | None
    created_at: datetime
    updated_at: datetime
    has_managed_database: bool

    @classmethod
    def from_project(cls, project: Project) -> ProjectRead:
        values = asdict(project)
        values["deployment_config"] = _public_deployment_config(project.deployment_config)
        return cls(**values)


class ProjectList(ApiModel):
    items: list[ProjectRead]


class CommitRead(ApiModel):
    sha: str
    author_name: str
    committed_at: datetime
    subject: str


class CommitList(ApiModel):
    items: list[CommitRead]


def _public_deployment_config(config: dict | None) -> dict | None:
    if config is None:
        return None
    public = {"services": [], "routes": list(config.get("routes", []))}
    for service in config.get("services", []):
        item = {key: value for key, value in service.items() if key != "environment"}
        item["environment"] = []
        for variable in service.get("environment", []):
            if variable.get("kind") == EnvironmentVariableKind.SECRET.value:
                item["environment"].append(
                    {"name": variable["name"], "kind": "SECRET", "configured": True}
                )
            else:
                item["environment"].append(
                    {"name": variable["name"], "kind": "PLAIN", "value": variable["value"]}
                )
        public["services"].append(item)
    return public
