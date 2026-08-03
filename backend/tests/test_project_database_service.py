from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from conftest import FakeGit, MemoryProjects, MemorySecretStore
from test_project_schemas import valid_settings

from heimdall.project_database.models import (
    ProjectDatabasePhase,
    ProjectDatabaseResource,
    ProjectDatabaseStatus,
)
from heimdall.project_database.service import ProjectDatabaseService
from heimdall.projects.schemas import ProjectCreate, ProjectSettingsUpdate
from heimdall.projects.service import ProjectService
from heimdall.secrets.store import StoredSecret


class MemoryProjectDatabaseRepository:
    def __init__(self) -> None:
        self.resource: ProjectDatabaseResource | None = None

    def get_for_project(self, project_id: UUID) -> ProjectDatabaseResource | None:
        if self.resource is not None and self.resource.project_id == project_id:
            return self.resource
        return None

    def ensure_intent(self, project_id: UUID) -> ProjectDatabaseResource:
        if self.resource is None:
            now = datetime.now(UTC)
            resource_id = uuid4()
            self.resource = ProjectDatabaseResource(
                id=resource_id,
                project_id=project_id,
                desired_state="ACTIVE",
                status=ProjectDatabaseStatus.PROVISIONING,
                phase=ProjectDatabasePhase.INTENT_RECORDED,
                database_name=f"hd_db_{resource_id.hex}",
                role_name=f"hd_role_{resource_id.hex}",
                schema_name="app",
                credential_reference=None,
                credential_version=None,
                credential_fingerprint=None,
                state_version=0,
                failure_stage=None,
                failure_code=None,
                created_at=now,
                updated_at=now,
            )
        return self.resource

    def begin_retry(self, resource: ProjectDatabaseResource) -> ProjectDatabaseResource:
        return self._save(
            replace(
                resource,
                status=ProjectDatabaseStatus.PROVISIONING,
                failure_stage=None,
                failure_code=None,
            )
        )

    def record_secret(
        self, resource: ProjectDatabaseResource, secret: StoredSecret
    ) -> ProjectDatabaseResource:
        return self._save(
            replace(
                resource,
                phase=ProjectDatabasePhase.SECRET_READY,
                credential_reference=secret.reference,
                credential_version=secret.version,
                credential_fingerprint=secret.fingerprint,
            )
        )

    def advance(
        self, resource: ProjectDatabaseResource, phase: ProjectDatabasePhase
    ) -> ProjectDatabaseResource:
        return self._save(
            replace(
                resource,
                phase=phase,
                status=(
                    ProjectDatabaseStatus.ACTIVE
                    if phase is ProjectDatabasePhase.ACTIVE
                    else resource.status
                ),
            )
        )

    def mark_failed(
        self, resource: ProjectDatabaseResource, stage: str, code: str
    ) -> ProjectDatabaseResource:
        return self._save(
            replace(
                resource,
                status=ProjectDatabaseStatus.FAILED,
                failure_stage=stage,
                failure_code=code,
            )
        )

    def _save(self, resource: ProjectDatabaseResource) -> ProjectDatabaseResource:
        self.resource = replace(
            resource, state_version=resource.state_version + 1, updated_at=datetime.now(UTC)
        )
        return self.resource


class FakeProvisioner:
    def __init__(self) -> None:
        self.steps: list[str] = []

    def ensure_role(self, _: UUID, __: str, password: str) -> None:
        assert password == "generated-secret-v1"
        self.steps.append("role")

    def ensure_database(self, _: UUID, __: str) -> None:
        self.steps.append("database")

    def ensure_privileges(self, _: str, __: str, ___: str) -> None:
        self.steps.append("privileges")

    def verify_login(self, _: str, __: str, ___: str, password: str) -> None:
        assert password == "generated-secret-v1"
        self.steps.append("login")


def ready_database_project() -> tuple[ProjectService, object]:
    projects = ProjectService(MemoryProjects(), FakeGit())
    project = projects.create(
        ProjectCreate(name="Console", repositoryUrl="https://github.com/example/console")
    )
    payload = valid_settings()
    payload["services"][1]["projectDatabaseAccess"] = True
    project = projects.update_settings(project.id, ProjectSettingsUpdate.model_validate(payload))
    return projects, project


def test_project_database_is_provisioned_to_active_without_returning_password() -> None:
    projects, project = ready_database_project()
    repository = MemoryProjectDatabaseRepository()
    provisioner = FakeProvisioner()
    service = ProjectDatabaseService(
        repository,
        projects,
        MemorySecretStore(),
        provisioner,
        "managed-postgres",
        5432,
    )

    result = service.provision(project.id)

    assert result.status == "ACTIVE"
    assert result.connected_services == ["api"]
    assert result.host == "managed-postgres"
    assert provisioner.steps == ["role", "database", "privileges", "login"]
    assert "password" not in result.model_dump_json().lower()
