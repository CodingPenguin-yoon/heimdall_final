from contextlib import contextmanager
from dataclasses import replace

import pytest
from conftest import FakeGit, MemoryProjects, MemorySecretStore
from test_project_schemas import valid_settings

from heimdall.common.errors import AppError
from heimdall.projects.models import ProjectStatus
from heimdall.projects.schemas import ProjectCreate, ProjectRead, ProjectSettingsUpdate
from heimdall.projects.service import ProjectService


class LockRequiredSecretStore(MemorySecretStore):
    def __init__(self) -> None:
        super().__init__()
        self.locked = False

    @contextmanager
    def project_operation_lock(self, _project_id, *, blocking: bool = True):
        assert blocking is True
        self.locked = True
        try:
            yield
        finally:
            self.locked = False

    def create(self, reference_root, version, value=None):
        assert self.locked
        return super().create(reference_root, version, value)


def test_registration_creates_draft_after_main_validation() -> None:
    repository = MemoryProjects()
    git = FakeGit()
    service = ProjectService(repository, git)

    project = service.create(
        ProjectCreate(name="Console", repositoryUrl="https://github.com/example/console")
    )

    assert project.status is ProjectStatus.DRAFT
    assert project.config_version == 0
    assert git.validated == ["https://github.com/example/console"]


def test_settings_promote_project_to_ready() -> None:
    repository = MemoryProjects()
    service = ProjectService(repository, FakeGit())
    project = service.create(
        ProjectCreate(name="Console", repositoryUrl="https://github.com/example/console")
    )

    ready = service.update_settings(
        project.id, ProjectSettingsUpdate.model_validate(valid_settings())
    )

    assert ready.status is ProjectStatus.READY
    assert ready.config_version == 1
    assert ready.deployment_config["services"][1]["name"] == "api"


def test_stale_settings_are_rejected() -> None:
    repository = MemoryProjects()
    service = ProjectService(repository, FakeGit())
    project = service.create(
        ProjectCreate(name="Console", repositoryUrl="https://github.com/example/console")
    )
    request = ProjectSettingsUpdate.model_validate(valid_settings())
    service.update_settings(project.id, request)

    with pytest.raises(AppError) as raised:
        service.update_settings(project.id, request)

    assert raised.value.code == "PROJECT_VERSION_CONFLICT"


def test_secret_environment_is_stored_as_reference_and_redacted_from_response() -> None:
    repository = MemoryProjects()
    secret_store = MemorySecretStore()
    service = ProjectService(repository, FakeGit(), secret_store)
    project = service.create(
        ProjectCreate(name="Console", repositoryUrl="https://github.com/example/console")
    )
    payload = valid_settings()
    payload["services"][1]["environment"] = [
        {"name": "APP_ENV", "kind": "PLAIN", "value": "production"},
        {"name": "JWT_SECRET", "kind": "SECRET", "value": "never-return-this"},
    ]

    updated = service.update_settings(project.id, ProjectSettingsUpdate.model_validate(payload))
    response = ProjectRead.from_project(updated).model_dump(mode="json", by_alias=True)

    snapshot = updated.deployment_config["services"][1]["environment"]
    assert snapshot[0]["value"] == "production"
    assert snapshot[1]["secretReference"].endswith("v1.secret")
    assert snapshot[1]["secretFingerprint"] == f"{1:064x}"
    assert "never-return-this" not in str(updated.deployment_config)
    assert response["deploymentConfig"]["services"][1]["environment"][1] == {
        "name": "JWT_SECRET",
        "kind": "SECRET",
        "configured": True,
    }


def test_settings_hold_the_project_secret_operation_lock_while_writing() -> None:
    repository = MemoryProjects()
    secret_store = LockRequiredSecretStore()
    service = ProjectService(repository, FakeGit(), secret_store)
    project = service.create(
        ProjectCreate(name="Console", repositoryUrl="https://github.com/example/console")
    )
    payload = valid_settings()
    payload["services"][1]["environment"] = [
        {"name": "JWT_SECRET", "kind": "SECRET", "value": "never-orphan-this"}
    ]

    updated = service.update_settings(project.id, ProjectSettingsUpdate.model_validate(payload))

    assert updated.status is ProjectStatus.READY
    assert secret_store.locked is False


def test_project_read_exposes_managed_database_resource_presence() -> None:
    repository = MemoryProjects()
    service = ProjectService(repository, FakeGit())
    project = service.create(
        ProjectCreate(name="Console", repositoryUrl="https://github.com/example/console")
    )

    response = ProjectRead.from_project(replace(project, has_managed_database=True)).model_dump(
        mode="json", by_alias=True
    )

    assert response["hasManagedDatabase"] is True


def test_secret_value_can_be_preserved_on_settings_update() -> None:
    repository = MemoryProjects()
    secret_store = MemorySecretStore()
    service = ProjectService(repository, FakeGit(), secret_store)
    project = service.create(
        ProjectCreate(name="Console", repositoryUrl="https://github.com/example/console")
    )
    payload = valid_settings()
    payload["services"][1]["environment"] = [
        {"name": "JWT_SECRET", "kind": "SECRET", "value": "first-value"}
    ]
    first = service.update_settings(project.id, ProjectSettingsUpdate.model_validate(payload))
    payload["expectedVersion"] = first.config_version
    payload["services"][1]["environment"][0].pop("value")

    second = service.update_settings(project.id, ProjectSettingsUpdate.model_validate(payload))

    variable = second.deployment_config["services"][1]["environment"][0]
    assert variable["secretVersion"] == 1
