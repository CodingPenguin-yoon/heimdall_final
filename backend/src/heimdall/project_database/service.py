from __future__ import annotations

from uuid import UUID

from heimdall.common.errors import AppError
from heimdall.project_database.models import (
    ProjectDatabasePhase,
    ProjectDatabaseProjectDeletingError,
    ProjectDatabaseProvisioningError,
    ProjectDatabaseResource,
    ProjectDatabaseStatus,
    ProjectDatabaseVersionConflict,
)
from heimdall.project_database.provisioner import PostgresProjectDatabaseProvisioner
from heimdall.project_database.repository import ProjectDatabaseRepository
from heimdall.project_database.schemas import ProjectDatabaseRead
from heimdall.projects.models import Project
from heimdall.projects.service import ProjectService
from heimdall.secrets.store import SecretStore, SecretStoreError


class ProjectDatabaseService:
    def __init__(
        self,
        repository: ProjectDatabaseRepository,
        projects: ProjectService,
        secret_store: SecretStore,
        provisioner: PostgresProjectDatabaseProvisioner | None,
        runtime_host: str,
        runtime_port: int,
    ) -> None:
        self._repository = repository
        self._projects = projects
        self._secret_store = secret_store
        self._provisioner = provisioner
        self._runtime_host = runtime_host
        self._runtime_port = runtime_port

    def status(self, project_id: UUID) -> ProjectDatabaseRead:
        project = self._projects.get(project_id)
        connected_services = _connected_services(project)
        if not connected_services:
            raise AppError(
                409,
                "PROJECT_DATABASE_NOT_REQUESTED",
                "Enable PostgreSQL for at least one service first",
            )
        resource = self._repository.get_for_project(project_id)
        if resource is None:
            return ProjectDatabaseRead.not_created(connected_services)
        return self._read(resource, connected_services)

    def provision(self, project_id: UUID) -> ProjectDatabaseRead:
        project = self._projects.ready(project_id)
        connected_services = _connected_services(project)
        if not connected_services:
            raise AppError(
                409,
                "PROJECT_DATABASE_NOT_REQUESTED",
                "Enable PostgreSQL for at least one service first",
            )
        if self._provisioner is None:
            raise AppError(
                503,
                "PROJECT_DATABASE_DISABLED",
                "Managed project PostgreSQL is not configured",
            )

        try:
            resource = self._repository.ensure_intent(project_id)
        except ProjectDatabaseProjectDeletingError as error:
            raise AppError(409, "PROJECT_DELETING", "Project deletion is in progress") from error
        try:
            with (
                self._provisioner.operation_lock(resource.id),
                self._projects.locked_ready(project_id) as project,
            ):
                connected_services = _connected_services(project)
                if resource.status is ProjectDatabaseStatus.ACTIVE:
                    return self._read(resource, connected_services)
                if resource.status is ProjectDatabaseStatus.FAILED:
                    resource = self._repository.begin_retry(resource)
                resource = self._reconcile(resource)
        except ProjectDatabaseVersionConflict as error:
            raise AppError(
                409,
                "PROJECT_DATABASE_CONFLICT",
                "Database provisioning changed; reload its status",
            ) from error
        except (ProjectDatabaseProvisioningError, SecretStoreError) as error:
            stage = error.stage if isinstance(error, ProjectDatabaseProvisioningError) else "SECRET"
            code = (
                error.code
                if isinstance(error, ProjectDatabaseProvisioningError)
                else "SECRET_FAILED"
            )
            latest = self._repository.get_for_project(project_id) or resource
            self._repository.mark_failed(latest, stage, code)
            raise AppError(
                503,
                "PROJECT_DATABASE_PROVISIONING_FAILED",
                "Managed PostgreSQL provisioning did not complete",
            ) from error
        return self._read(resource, connected_services)

    def require_active(self, project: Project) -> None:
        if not _connected_services(project):
            return
        resource = self._repository.get_for_project(project.id)
        if resource is None or resource.status is not ProjectDatabaseStatus.ACTIVE:
            raise AppError(
                409,
                "PROJECT_DATABASE_NOT_ACTIVE",
                "Provision the project database before deployment",
            )

    def deployment_metadata(self, project: Project) -> dict | None:
        if not _connected_services(project):
            return None
        self.require_active(project)
        resource = self._repository.get_for_project(project.id)
        if (
            resource is None
            or resource.credential_reference is None
            or resource.credential_version is None
            or resource.credential_fingerprint is None
        ):
            raise AppError(
                409,
                "PROJECT_DATABASE_CREDENTIAL_NOT_READY",
                "Project database credential metadata is incomplete",
            )
        return {
            "resourceId": str(resource.id),
            "databaseName": resource.database_name,
            "username": resource.role_name,
            "schemaName": resource.schema_name,
            "host": self._runtime_host,
            "port": self._runtime_port,
            "credentialReference": resource.credential_reference,
            "credentialVersion": resource.credential_version,
            "credentialFingerprint": resource.credential_fingerprint,
        }

    def _reconcile(self, resource: ProjectDatabaseResource) -> ProjectDatabaseResource:
        if resource.phase is ProjectDatabasePhase.INTENT_RECORDED:
            secret = self._secret_store.create(
                f"projects/{resource.project_id}/database/{resource.id}/credentials", 1
            )
            resource = self._repository.record_secret(resource, secret)

        password = self._credential(resource)
        if resource.phase is ProjectDatabasePhase.SECRET_READY:
            self._provisioner.ensure_role(resource.id, resource.role_name, password)
            resource = self._repository.advance(resource, ProjectDatabasePhase.ROLE_READY)
        if resource.phase is ProjectDatabasePhase.ROLE_READY:
            self._provisioner.ensure_database(resource.id, resource.database_name)
            resource = self._repository.advance(resource, ProjectDatabasePhase.DATABASE_READY)
        if resource.phase is ProjectDatabasePhase.DATABASE_READY:
            self._provisioner.ensure_privileges(
                resource.database_name, resource.role_name, resource.schema_name
            )
            resource = self._repository.advance(resource, ProjectDatabasePhase.PRIVILEGES_READY)
        if resource.phase is ProjectDatabasePhase.PRIVILEGES_READY:
            self._provisioner.verify_login(
                resource.database_name, resource.role_name, resource.schema_name, password
            )
            resource = self._repository.advance(resource, ProjectDatabasePhase.ACTIVE)
        return resource

    def _credential(self, resource: ProjectDatabaseResource) -> str:
        if resource.credential_reference is None or resource.credential_fingerprint is None:
            raise SecretStoreError("database credential metadata is incomplete")
        return self._secret_store.read(
            resource.credential_reference, resource.credential_fingerprint
        )

    def _read(
        self, resource: ProjectDatabaseResource, connected_services: list[str]
    ) -> ProjectDatabaseRead:
        return ProjectDatabaseRead.from_resource(
            resource,
            self._runtime_host,
            self._runtime_port,
            connected_services,
        )


def _connected_services(project: Project) -> list[str]:
    config = project.deployment_config or {}
    return sorted(
        service["name"]
        for service in config.get("services", [])
        if service.get("projectDatabaseAccess") is True
    )
