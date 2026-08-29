from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from heimdall.common.errors import AppError
from heimdall.deployments.diagnostics import (
    DeploymentDiagnosticArtifact,
    DeploymentDiagnosticNotFoundError,
    DiagnosticArtifactDraft,
)
from heimdall.deployments.event_stream import (
    DeploymentEventStreamEnd,
    DeploymentEventStreamError,
    DeploymentEventStreamReady,
)
from heimdall.deployments.models import (
    ActiveDeploymentError,
    Deployment,
    DeploymentEvent,
    DeploymentNotFoundError,
    DeploymentProjectDeletingError,
    DeploymentReconciliationConflictError,
    DeploymentSource,
)
from heimdall.deployments.repository import DeploymentRepository
from heimdall.deployments.schemas import DeploymentCreate
from heimdall.project_database.service import ProjectDatabaseService
from heimdall.projects.service import ProjectService
from heimdall.runtime.logs import (
    ServiceLogError,
    ServiceLogSnapshot,
    ServiceLogStreamEvent,
    ServiceLogStreamReady,
)


class ServiceLogGateway(Protocol):
    def fetch(self, deployment_id: UUID, service_name: str | None) -> ServiceLogSnapshot: ...


class ServiceLogStreamSubscription(Protocol):
    ready: ServiceLogStreamReady

    def receive(self) -> ServiceLogStreamEvent | None: ...

    def close(self) -> None: ...


class ServiceLogStreamGateway(Protocol):
    def open(
        self, deployment_id: UUID, service_name: str | None
    ) -> ServiceLogStreamSubscription: ...


class DeploymentEventStreamSubscription(Protocol):
    ready: DeploymentEventStreamReady

    def receive(self) -> DeploymentEvent | DeploymentEventStreamEnd | None: ...

    def close(self) -> None: ...


class DeploymentEventStreamGateway(Protocol):
    def open(self, deployment_id: UUID, after_id: int) -> DeploymentEventStreamSubscription: ...


_SERVICE_LOG_ERRORS = {
    "SERVICE_LOG_SERVICE_NOT_FOUND": (
        400,
        "SERVICE_LOG_SERVICE_NOT_FOUND",
        "Select a service from the immutable deployment snapshot",
    ),
    "SERVICE_LOGS_UNAVAILABLE": (
        409,
        "SERVICE_LOGS_UNAVAILABLE",
        "The service container has not been created or is no longer available",
    ),
    "RUNTIME_LOG_BROKER_UNAVAILABLE": (
        503,
        "RUNTIME_LOG_BROKER_UNAVAILABLE",
        "The runtime Worker log broker is unavailable",
    ),
    "SERVICE_LOG_REDACTION_UNAVAILABLE": (
        503,
        "SERVICE_LOG_REDACTION_UNAVAILABLE",
        "Service logs were withheld because secret redaction could not be prepared",
    ),
    "RUNTIME_LOG_STREAM_BUSY": (
        503,
        "RUNTIME_LOG_STREAM_BUSY",
        "The runtime Worker has reached the live service log connection limit",
    ),
    "RUNTIME_LOG_STREAM_UNAVAILABLE": (
        503,
        "RUNTIME_LOG_STREAM_UNAVAILABLE",
        "The runtime Worker live log broker is unavailable",
    ),
}

_DEPLOYMENT_EVENT_STREAM_ERRORS = {
    "DEPLOYMENT_EVENT_STREAM_BUSY": (
        503,
        "DEPLOYMENT_EVENT_STREAM_BUSY",
        "The deployment event stream connection limit has been reached",
    ),
    "DEPLOYMENT_EVENT_STREAM_UNAVAILABLE": (
        503,
        "DEPLOYMENT_EVENT_STREAM_UNAVAILABLE",
        "The deployment event stream is unavailable",
    ),
}


class DeploymentService:
    def __init__(
        self,
        repository: DeploymentRepository,
        projects: ProjectService,
        project_databases: ProjectDatabaseService | None = None,
        service_logs: ServiceLogGateway | None = None,
        service_log_stream: ServiceLogStreamGateway | None = None,
        deployment_event_stream: DeploymentEventStreamGateway | None = None,
    ) -> None:
        self._repository = repository
        self._projects = projects
        self._project_databases = project_databases
        self._service_logs = service_logs
        self._service_log_stream = service_log_stream
        self._deployment_event_stream = deployment_event_stream

    def request(self, project_id: UUID, request: DeploymentCreate) -> Deployment:
        project = self._projects.ready(project_id)
        database_metadata = None
        if self._project_databases is not None:
            database_metadata = self._project_databases.deployment_metadata(project)
        elif any(
            service.get("projectDatabaseAccess") is True
            for service in (project.deployment_config or {}).get("services", [])
        ):
            raise AppError(
                503,
                "PROJECT_DATABASE_UNAVAILABLE",
                "Managed project PostgreSQL is not configured",
            )
        commits = self._projects.commits(project_id)
        if not commits:
            raise AppError(422, "MAIN_HAS_NO_COMMITS", "The main branch has no commits")

        requested_sha = request.source.commit_sha
        if request.source.type is DeploymentSource.MAIN_HEAD:
            resolved_sha = commits[0].sha
        else:
            matched = next((commit for commit in commits if commit.sha == requested_sha), None)
            if matched is None:
                raise AppError(
                    422,
                    "COMMIT_NOT_IN_RECENT_MAIN",
                    "Select a commit from the recent main commit list",
                )
            resolved_sha = matched.sha

        try:
            config_snapshot = deepcopy(project.deployment_config or {})
            if database_metadata is not None:
                config_snapshot["managedDatabase"] = database_metadata
            return self._repository.create(
                project_id=project.id,
                source_type=request.source.type,
                requested_commit_sha=requested_sha,
                resolved_commit_sha=resolved_sha,
                config_version=project.config_version,
                config_snapshot=config_snapshot,
            )
        except ActiveDeploymentError as error:
            raise AppError(
                409, "ACTIVE_DEPLOYMENT_EXISTS", "Wait for the active deployment to finish"
            ) from error
        except DeploymentProjectDeletingError as error:
            raise AppError(409, "PROJECT_DELETING", "Project deletion is in progress") from error

    def list_for_project(self, project_id: UUID) -> Sequence[Deployment]:
        self._projects.get(project_id)
        return self._repository.list_for_project(project_id)

    def list_recent(self) -> Sequence[Deployment]:
        return self._repository.list_recent(limit=100)

    def get(self, deployment_id: UUID) -> Deployment:
        try:
            return self._repository.get(deployment_id)
        except DeploymentNotFoundError as error:
            raise AppError(404, "DEPLOYMENT_NOT_FOUND", "Deployment was not found") from error

    def list_uncertain_before(self, cutoff: datetime) -> Sequence[Deployment]:
        return self._repository.list_uncertain_before(cutoff)

    def reconcile_succeeded(self, deployment_id: UUID) -> Deployment:
        try:
            return self._repository.reconcile_succeeded(deployment_id)
        except DeploymentNotFoundError as error:
            raise AppError(404, "DEPLOYMENT_NOT_FOUND", "Deployment was not found") from error
        except DeploymentReconciliationConflictError as error:
            raise AppError(
                409,
                "DEPLOYMENT_RECONCILIATION_CONFLICT",
                "Deployment state cannot be reconciled as active",
            ) from error

    def events(self, deployment_id: UUID) -> Sequence[DeploymentEvent]:
        self.get(deployment_id)
        return self._repository.list_events(deployment_id)

    def diagnostics(self, deployment_id: UUID) -> Sequence[DeploymentDiagnosticArtifact]:
        self.get(deployment_id)
        return self._repository.list_diagnostics(deployment_id)

    def diagnostic(
        self,
        deployment_id: UUID,
        artifact_id: UUID,
    ) -> DeploymentDiagnosticArtifact:
        self.get(deployment_id)
        try:
            return self._repository.get_diagnostic(deployment_id, artifact_id)
        except DeploymentDiagnosticNotFoundError as error:
            raise AppError(
                404,
                "DEPLOYMENT_DIAGNOSTIC_NOT_FOUND",
                "Deployment diagnostic was not found or has expired",
            ) from error

    def record_reconciliation_diagnostics(
        self,
        deployment_id: UUID,
        *,
        failure_stage: str,
        failure_code: str,
        artifacts: Sequence[DiagnosticArtifactDraft],
        retention: timedelta,
    ) -> DeploymentEvent:
        return self._repository.record_reconciliation_diagnostics(
            deployment_id,
            failure_stage=failure_stage,
            failure_code=failure_code,
            artifacts=artifacts,
            retention=retention,
        )

    def open_event_stream(
        self,
        deployment_id: UUID,
        after_id: int,
    ) -> DeploymentEventStreamSubscription:
        self.get(deployment_id)
        if self._deployment_event_stream is None:
            raise AppError(
                503,
                "DEPLOYMENT_EVENT_STREAM_UNAVAILABLE",
                "The deployment event stream is unavailable",
            )
        try:
            return self._deployment_event_stream.open(deployment_id, after_id)
        except DeploymentEventStreamError as error:
            status, code, message = _DEPLOYMENT_EVENT_STREAM_ERRORS.get(
                error.code,
                _DEPLOYMENT_EVENT_STREAM_ERRORS["DEPLOYMENT_EVENT_STREAM_UNAVAILABLE"],
            )
            raise AppError(status, code, message) from error

    def service_logs(
        self,
        deployment_id: UUID,
        service_name: str | None,
    ) -> ServiceLogSnapshot:
        self.get(deployment_id)
        if self._service_logs is None:
            raise AppError(
                503,
                "RUNTIME_LOG_BROKER_UNAVAILABLE",
                "The runtime Worker log broker is unavailable",
            )
        try:
            return self._service_logs.fetch(deployment_id, service_name)
        except ServiceLogError as error:
            status, code, message = _SERVICE_LOG_ERRORS.get(
                error.code,
                _SERVICE_LOG_ERRORS["RUNTIME_LOG_BROKER_UNAVAILABLE"],
            )
            raise AppError(status, code, message) from error

    def open_service_log_stream(
        self,
        deployment_id: UUID,
        service_name: str | None,
    ) -> ServiceLogStreamSubscription:
        self.get(deployment_id)
        if self._service_log_stream is None:
            raise AppError(
                503,
                "RUNTIME_LOG_STREAM_UNAVAILABLE",
                "The runtime Worker live log broker is unavailable",
            )
        try:
            return self._service_log_stream.open(deployment_id, service_name)
        except ServiceLogError as error:
            status, code, message = _SERVICE_LOG_ERRORS.get(
                error.code,
                _SERVICE_LOG_ERRORS["RUNTIME_LOG_STREAM_UNAVAILABLE"],
            )
            raise AppError(status, code, message) from error
