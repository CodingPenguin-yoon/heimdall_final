from __future__ import annotations

import re
from datetime import datetime
from uuid import UUID

from pydantic import model_validator

from heimdall.common.api_model import ApiModel
from heimdall.deployments.models import Deployment, DeploymentSource, DeploymentStatus

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


class DeploymentSourceInput(ApiModel):
    type: DeploymentSource
    commit_sha: str | None = None

    @model_validator(mode="after")
    def validate_source(self) -> DeploymentSourceInput:
        if self.type is DeploymentSource.MAIN_HEAD and self.commit_sha is not None:
            raise ValueError("MAIN_HEAD must not include commitSha")
        if self.type is DeploymentSource.MAIN_COMMIT and (
            self.commit_sha is None or not FULL_SHA.fullmatch(self.commit_sha)
        ):
            raise ValueError("MAIN_COMMIT requires a lowercase full commit SHA")
        return self


class DeploymentCreate(ApiModel):
    source: DeploymentSourceInput


class DeploymentRead(ApiModel):
    id: UUID
    project_id: UUID
    source_type: DeploymentSource
    requested_commit_sha: str | None
    resolved_commit_sha: str
    config_version: int
    status: DeploymentStatus
    failure_stage: str | None
    failure_code: str | None
    created_at: datetime
    updated_at: datetime
    terminal_at: datetime | None

    @classmethod
    def from_deployment(cls, deployment: Deployment) -> DeploymentRead:
        return cls(
            id=deployment.id,
            project_id=deployment.project_id,
            source_type=deployment.source_type,
            requested_commit_sha=deployment.requested_commit_sha,
            resolved_commit_sha=deployment.resolved_commit_sha,
            config_version=deployment.config_version,
            status=deployment.status,
            failure_stage=deployment.failure_stage,
            failure_code=deployment.failure_code,
            created_at=deployment.created_at,
            updated_at=deployment.updated_at,
            terminal_at=deployment.terminal_at,
        )


class DeploymentList(ApiModel):
    items: list[DeploymentRead]
