from __future__ import annotations

from collections.abc import Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from heimdall.common.errors import AppError
from heimdall.git.client import Commit, GitAccessError, GitClient
from heimdall.projects.models import (
    Project,
    ProjectConflictError,
    ProjectDeletionConflictError,
    ProjectDeletionJob,
    ProjectDeletionNotFoundError,
    ProjectDeletionValidationError,
    ProjectEnvironmentSecret,
    ProjectNotFoundError,
    ProjectStatus,
    ProjectVersionConflictError,
)
from heimdall.projects.repository import ProjectRepository
from heimdall.projects.schemas import (
    EnvironmentVariableKind,
    ProjectCreate,
    ProjectDeletionRequest,
    ProjectSettingsUpdate,
)
from heimdall.secrets.store import SecretStore, SecretStoreError


class ProjectService:
    def __init__(
        self, repository: ProjectRepository, git: GitClient, secret_store: SecretStore | None = None
    ) -> None:
        self._repository = repository
        self._git = git
        self._secret_store = secret_store

    def create(self, request: ProjectCreate) -> Project:
        try:
            self._git.validate_main(request.repository_url)
            return self._repository.create(request.name, request.repository_url)
        except GitAccessError as error:
            raise AppError(422, "REPOSITORY_UNAVAILABLE", str(error)) from error
        except ProjectConflictError as error:
            raise AppError(
                409, "PROJECT_CONFLICT", "Project name or repository already exists"
            ) from error

    def list(self) -> Sequence[Project]:
        return self._repository.list()

    def get(self, project_id: UUID) -> Project:
        try:
            return self._repository.get(project_id)
        except ProjectNotFoundError as error:
            raise AppError(404, "PROJECT_NOT_FOUND", "Project was not found") from error

    def ready(self, project_id: UUID) -> Project:
        return self._ensure_ready(self.get(project_id))

    @contextmanager
    def locked_ready(self, project_id: UUID):
        try:
            with self._repository.lock_for_external_operation(project_id) as project:
                yield self._ensure_ready(project)
        except ProjectNotFoundError as error:
            raise AppError(404, "PROJECT_NOT_FOUND", "Project was not found") from error

    @staticmethod
    def _ensure_ready(project: Project) -> Project:
        if project.status is ProjectStatus.DELETING:
            raise AppError(409, "PROJECT_DELETING", "Project deletion is in progress")
        if project.status is not ProjectStatus.READY or project.deployment_config is None:
            raise AppError(409, "PROJECT_NOT_READY", "Complete project settings before deployment")
        return project

    def delete(self, project_id: UUID, request: ProjectDeletionRequest) -> ProjectDeletionJob:
        return self._delete_operation(self._repository.request_deletion, project_id, request)

    def deletion(self, project_id: UUID) -> ProjectDeletionJob:
        try:
            return self._repository.get_deletion(project_id)
        except ProjectNotFoundError as error:
            raise AppError(404, "PROJECT_NOT_FOUND", "Project was not found") from error
        except ProjectDeletionNotFoundError as error:
            raise AppError(
                404,
                "PROJECT_DELETION_NOT_FOUND",
                "Project deletion was not requested",
            ) from error

    def retry_deletion(
        self, project_id: UUID, request: ProjectDeletionRequest
    ) -> ProjectDeletionJob:
        return self._delete_operation(self._repository.retry_deletion, project_id, request)

    @staticmethod
    def _delete_operation(operation, project_id, request) -> ProjectDeletionJob:
        try:
            return operation(
                project_id,
                confirmation=request.confirmation,
                delete_managed_database=request.delete_managed_database,
                managed_database_confirmation=request.managed_database_confirmation,
            )
        except ProjectNotFoundError as error:
            raise AppError(404, "PROJECT_NOT_FOUND", "Project was not found") from error
        except ProjectDeletionNotFoundError as error:
            raise AppError(
                404,
                "PROJECT_DELETION_NOT_FOUND",
                "Project deletion was not requested",
            ) from error
        except ProjectDeletionValidationError as error:
            messages = {
                "PROJECT_DELETE_CONFIRMATION_MISMATCH": (
                    "Enter the full project ID to confirm deletion"
                ),
                "PROJECT_DATABASE_DELETE_CONFIRMATION_REQUIRED": (
                    "Confirm permanent deletion of managed PostgreSQL application data"
                ),
            }
            raise AppError(422, error.code, messages[error.code]) from error
        except ProjectDeletionConflictError as error:
            messages = {
                "PROJECT_DELETION_FAILED": (
                    "Project deletion failed; use the explicit retry endpoint"
                ),
                "PROJECT_DELETION_NOT_FAILED": "Only a failed project deletion can be retried",
            }
            raise AppError(409, error.code, messages[error.code]) from error

    def update_settings(self, project_id: UUID, request: ProjectSettingsUpdate) -> Project:
        try:
            if self._secret_store is None:
                return self._update_settings(project_id, request)
            with self._secret_store.project_operation_lock(project_id):
                return self._update_settings(project_id, request)
        except ProjectNotFoundError as error:
            raise AppError(404, "PROJECT_NOT_FOUND", "Project was not found") from error
        except ProjectVersionConflictError as error:
            raise AppError(
                409, "PROJECT_VERSION_CONFLICT", "Reload the project and apply settings again"
            ) from error
        except ProjectDeletionConflictError as error:
            if error.code != "PROJECT_DELETING":
                raise
            raise AppError(409, "PROJECT_DELETING", "Project deletion is in progress") from error
        except SecretStoreError as error:
            raise AppError(
                500, "SECRET_STORAGE_FAILED", "Could not store the project secret safely"
            ) from error

    def _update_settings(self, project_id: UUID, request: ProjectSettingsUpdate) -> Project:
        current = self.get(project_id)
        if current.status is ProjectStatus.DELETING:
            raise AppError(409, "PROJECT_DELETING", "Project deletion is in progress")
        if current.config_version != request.expected_version:
            raise AppError(
                409, "PROJECT_VERSION_CONFLICT", "Reload the project and apply settings again"
            )
        deployment_config, environment_secrets = self._resolve_configuration(project_id, request)
        return self._repository.update_settings(
            project_id,
            request.expected_version,
            deployment_config,
            environment_secrets,
        )

    def commits(self, project_id: UUID) -> list[Commit]:
        project = self.get(project_id)
        try:
            return self._git.recent_commits(project.repository_url)
        except GitAccessError as error:
            raise AppError(503, "REPOSITORY_UNAVAILABLE", str(error)) from error

    def _resolve_configuration(
        self, project_id: UUID, request: ProjectSettingsUpdate
    ) -> tuple[dict[str, Any], list[ProjectEnvironmentSecret]]:
        services: list[dict[str, Any]] = []
        secrets: list[ProjectEnvironmentSecret] = []
        now = datetime.now(UTC)

        for service in request.services:
            service_snapshot = service.model_dump(
                mode="json", by_alias=True, exclude={"environment"}
            )
            environment: list[dict[str, Any]] = []
            for variable in service.environment:
                if variable.kind is EnvironmentVariableKind.PLAIN:
                    environment.append(
                        {"name": variable.name, "kind": "PLAIN", "value": variable.value}
                    )
                    continue

                current = self._repository.get_environment_secret(
                    project_id, service.name, variable.name
                )
                if variable.value is None:
                    if current is None:
                        raise AppError(
                            400,
                            "SECRET_VALUE_REQUIRED",
                            f"Provide a value for {service.name}.{variable.name}",
                        )
                    resolved = current
                else:
                    if self._secret_store is None:
                        raise AppError(
                            503,
                            "SECRET_STORAGE_UNAVAILABLE",
                            "Project secret storage is not configured",
                        )
                    version = 1 if current is None else current.secret_version + 1
                    stored = self._secret_store.create(
                        (
                            f"projects/{project_id}/environment/"
                            f"{service.name}/{variable.name.lower()}"
                        ),
                        version,
                        variable.value,
                    )
                    resolved = ProjectEnvironmentSecret(
                        project_id=project_id,
                        service_name=service.name,
                        variable_name=variable.name,
                        secret_reference=stored.reference,
                        secret_version=stored.version,
                        secret_fingerprint=stored.fingerprint,
                        created_at=current.created_at if current is not None else now,
                        updated_at=now,
                    )
                secrets.append(resolved)
                environment.append(
                    {
                        "name": variable.name,
                        "kind": "SECRET",
                        "secretReference": resolved.secret_reference,
                        "secretVersion": resolved.secret_version,
                        "secretFingerprint": resolved.secret_fingerprint,
                    }
                )
            service_snapshot["environment"] = environment
            services.append(service_snapshot)

        return (
            {
                "services": services,
                "routes": [
                    route.model_dump(mode="json", by_alias=True) for route in request.routes
                ],
            },
            secrets,
        )
