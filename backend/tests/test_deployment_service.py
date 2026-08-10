from datetime import UTC, datetime

import pytest
from conftest import FakeGit, MemoryDeployments, MemoryProjects
from test_project_schemas import valid_settings

from heimdall.common.errors import AppError
from heimdall.deployments.models import DeploymentSource
from heimdall.deployments.schemas import DeploymentCreate
from heimdall.deployments.service import DeploymentService
from heimdall.projects.schemas import ProjectCreate, ProjectSettingsUpdate
from heimdall.projects.service import ProjectService
from heimdall.runtime.logs import ServiceLogError, ServiceLogSnapshot


class ActiveProjectDatabase:
    def deployment_metadata(self, project) -> dict:
        return {
            "resourceId": "00000000-0000-0000-0000-000000000001",
            "databaseName": "hd_db_one",
            "username": "hd_role_one",
            "schemaName": "app",
            "host": "managed-postgres",
            "port": 5432,
            "credentialReference": "projects/p1/database/r1/credentials/v1.secret",
            "credentialVersion": 1,
            "credentialFingerprint": "f" * 64,
        }


class ServiceLogs:
    def __init__(self, error_code: str | None = None) -> None:
        self.error_code = error_code
        self.requests = []

    def fetch(self, deployment_id, service_name):
        self.requests.append((deployment_id, service_name))
        if self.error_code is not None:
            raise ServiceLogError(self.error_code)
        return ServiceLogSnapshot(
            deployment_id=deployment_id,
            services=("web", "api"),
            service_name=service_name or "web",
            retrieved_at=datetime(2026, 8, 7, 1, tzinfo=UTC),
            lines=(),
            truncated=False,
        )


def ready_project() -> tuple[ProjectService, object]:
    projects = ProjectService(MemoryProjects(), FakeGit())
    project = projects.create(
        ProjectCreate(name="Console", repositoryUrl="https://github.com/example/console")
    )
    project = projects.update_settings(
        project.id, ProjectSettingsUpdate.model_validate(valid_settings())
    )
    return projects, project


def test_main_head_is_resolved_and_snapshot_is_saved() -> None:
    projects, project = ready_project()
    service = DeploymentService(MemoryDeployments(), projects)

    deployment = service.request(
        project.id,
        DeploymentCreate.model_validate({"source": {"type": "MAIN_HEAD"}}),
    )

    assert deployment.source_type is DeploymentSource.MAIN_HEAD
    assert deployment.resolved_commit_sha == "a" * 40
    assert deployment.config_version == 1
    assert deployment.config_snapshot["services"][0]["name"] == "web"


def test_recent_main_commit_can_be_selected_exactly() -> None:
    projects, project = ready_project()
    service = DeploymentService(MemoryDeployments(), projects)

    deployment = service.request(
        project.id,
        DeploymentCreate.model_validate({"source": {"type": "MAIN_COMMIT", "commitSha": "b" * 40}}),
    )

    assert deployment.requested_commit_sha == "b" * 40
    assert deployment.resolved_commit_sha == "b" * 40


def test_database_metadata_is_frozen_into_deployment_snapshot() -> None:
    projects = ProjectService(MemoryProjects(), FakeGit())
    project = projects.create(
        ProjectCreate(name="Database", repositoryUrl="https://github.com/example/database")
    )
    payload = valid_settings()
    payload["services"][1]["projectDatabaseAccess"] = True
    project = projects.update_settings(project.id, ProjectSettingsUpdate.model_validate(payload))
    service = DeploymentService(MemoryDeployments(), projects, ActiveProjectDatabase())

    deployment = service.request(
        project.id,
        DeploymentCreate.model_validate({"source": {"type": "MAIN_HEAD"}}),
    )

    assert deployment.config_snapshot["managedDatabase"]["databaseName"] == "hd_db_one"
    assert "password" not in str(deployment.config_snapshot).lower()


def test_service_logs_are_requested_only_after_deployment_lookup() -> None:
    projects, project = ready_project()
    repository = MemoryDeployments()
    logs = ServiceLogs()
    service = DeploymentService(repository, projects, service_logs=logs)
    deployment = service.request(
        project.id,
        DeploymentCreate.model_validate({"source": {"type": "MAIN_HEAD"}}),
    )

    snapshot = service.service_logs(deployment.id, "api")

    assert snapshot.service_name == "api"
    assert logs.requests == [(deployment.id, "api")]


@pytest.mark.parametrize(
    ("error_code", "status"),
    [
        ("SERVICE_LOG_SERVICE_NOT_FOUND", 400),
        ("SERVICE_LOGS_UNAVAILABLE", 409),
        ("RUNTIME_LOG_BROKER_UNAVAILABLE", 503),
        ("SERVICE_LOG_REDACTION_UNAVAILABLE", 503),
        ("UNKNOWN_BROKER_ERROR", 503),
    ],
)
def test_service_log_failures_map_to_stable_http_errors(error_code: str, status: int) -> None:
    projects, project = ready_project()
    repository = MemoryDeployments()
    logs = ServiceLogs(error_code)
    service = DeploymentService(repository, projects, service_logs=logs)
    deployment = service.request(
        project.id,
        DeploymentCreate.model_validate({"source": {"type": "MAIN_HEAD"}}),
    )

    with pytest.raises(AppError) as raised:
        service.service_logs(deployment.id, None)

    assert raised.value.status == status
    assert raised.value.code == (
        error_code if error_code != "UNKNOWN_BROKER_ERROR" else "RUNTIME_LOG_BROKER_UNAVAILABLE"
    )
