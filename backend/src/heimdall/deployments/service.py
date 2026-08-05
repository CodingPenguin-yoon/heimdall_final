from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from datetime import datetime
from uuid import UUID

from heimdall.common.errors import AppError
from heimdall.deployments.models import (
    ActiveDeploymentError,
    Deployment,
    DeploymentEvent,
    DeploymentNotFoundError,
    DeploymentReconciliationConflictError,
    DeploymentSource,
)
from heimdall.deployments.repository import DeploymentRepository
from heimdall.deployments.schemas import DeploymentCreate
from heimdall.project_database.service import ProjectDatabaseService
from heimdall.projects.service import ProjectService


class DeploymentService:
    def __init__(
        self,
        repository: DeploymentRepository,
        projects: ProjectService,
        project_databases: ProjectDatabaseService | None = None,
    ) -> None:
        self._repository = repository
        self._projects = projects
        self._project_databases = project_databases

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

    def list_for_project(self, project_id: UUID) -> Sequence[Deployment]:
        self._projects.get(project_id)
        return self._repository.list_for_project(project_id)

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
