from __future__ import annotations

import re
from datetime import datetime
from uuid import UUID

from pydantic import model_validator

from heimdall.common.api_model import ApiModel
from heimdall.deployments.diagnostics import (
    DeploymentDiagnosticArtifact,
    DiagnosticArtifactKind,
    DiagnosticCaptureStatus,
    DiagnosticStream,
)
from heimdall.deployments.models import (
    Deployment,
    DeploymentEvent,
    DeploymentSource,
    DeploymentStatus,
)
from heimdall.runtime.logs import ServiceLogSnapshot, ServiceLogStream

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


class DeploymentEventRead(ApiModel):
    id: int
    deployment_id: UUID
    stage: str
    code: str
    message: str
    created_at: datetime

    @classmethod
    def from_event(cls, event: DeploymentEvent) -> DeploymentEventRead:
        return cls(
            id=event.id,
            deployment_id=event.deployment_id,
            stage=event.stage,
            code=event.code,
            message=event.message,
            created_at=event.created_at,
        )


class DeploymentEventList(ApiModel):
    items: list[DeploymentEventRead]


class DeploymentDiagnosticMetadataRead(ApiModel):
    id: UUID
    deployment_id: UUID
    event_id: int
    kind: DiagnosticArtifactKind
    failure_stage: str
    failure_code: str
    capture_status: DiagnosticCaptureStatus
    capture_code: str | None
    operation: str | None
    service_name: str | None
    return_code: int | None
    container_status: str | None
    container_exit_code: int | None
    line_count: int
    byte_count: int
    truncated: bool
    captured_at: datetime
    expires_at: datetime

    @classmethod
    def from_artifact(
        cls, artifact: DeploymentDiagnosticArtifact
    ) -> DeploymentDiagnosticMetadataRead:
        return cls(
            id=artifact.id,
            deployment_id=artifact.deployment_id,
            event_id=artifact.event_id,
            kind=artifact.kind,
            failure_stage=artifact.failure_stage,
            failure_code=artifact.failure_code,
            capture_status=artifact.capture_status,
            capture_code=artifact.capture_code,
            operation=artifact.operation,
            service_name=artifact.service_name,
            return_code=artifact.return_code,
            container_status=artifact.container_status,
            container_exit_code=artifact.container_exit_code,
            line_count=artifact.line_count,
            byte_count=artifact.byte_count,
            truncated=artifact.truncated,
            captured_at=artifact.captured_at,
            expires_at=artifact.expires_at,
        )


class DeploymentDiagnosticList(ApiModel):
    items: list[DeploymentDiagnosticMetadataRead]


class DeploymentDiagnosticLineRead(ApiModel):
    timestamp: str | None
    stream: DiagnosticStream
    message: str


class DeploymentDiagnosticRead(DeploymentDiagnosticMetadataRead):
    lines: list[DeploymentDiagnosticLineRead]

    @classmethod
    def from_artifact(cls, artifact: DeploymentDiagnosticArtifact) -> DeploymentDiagnosticRead:
        metadata = DeploymentDiagnosticMetadataRead.from_artifact(artifact)
        return cls(
            **metadata.model_dump(),
            lines=[
                DeploymentDiagnosticLineRead(
                    timestamp=line.timestamp,
                    stream=line.stream,
                    message=line.message,
                )
                for line in artifact.lines or ()
            ],
        )


class ServiceLogLineRead(ApiModel):
    timestamp: str
    stream: ServiceLogStream
    message: str


class ServiceLogRead(ApiModel):
    deployment_id: UUID
    services: list[str]
    service_name: str
    retrieved_at: datetime
    lines: list[ServiceLogLineRead]
    truncated: bool

    @classmethod
    def from_snapshot(cls, snapshot: ServiceLogSnapshot) -> ServiceLogRead:
        return cls(
            deployment_id=snapshot.deployment_id,
            services=list(snapshot.services),
            service_name=snapshot.service_name,
            retrieved_at=snapshot.retrieved_at,
            lines=[
                ServiceLogLineRead(
                    timestamp=line.timestamp,
                    stream=line.stream,
                    message=line.message,
                )
                for line in snapshot.lines
            ],
            truncated=snapshot.truncated,
        )
