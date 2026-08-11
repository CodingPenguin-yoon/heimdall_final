from __future__ import annotations

import json
import os
import shutil
from datetime import timedelta
from pathlib import Path
from urllib.request import urlopen
from uuid import uuid4

import pytest
from conftest import FakeGit

from heimdall.database import Database
from heimdall.deployments.models import DeploymentStatus
from heimdall.deployments.repository import PostgresDeploymentRepository
from heimdall.deployments.schemas import DeploymentCreate
from heimdall.deployments.service import DeploymentService
from heimdall.deployments.worker import DeploymentWorker
from heimdall.project_database.provisioner import PostgresProjectDatabaseProvisioner
from heimdall.project_database.repository import PostgresProjectDatabaseRepository
from heimdall.project_database.service import ProjectDatabaseService
from heimdall.projects.repository import PostgresProjectRepository
from heimdall.projects.schemas import ProjectCreate, ProjectSettingsUpdate
from heimdall.projects.service import ProjectService
from heimdall.runtime.docker import DockerRuntime, HttpHealthProbe
from heimdall.runtime.docker_logs import DockerServiceLogReader
from heimdall.runtime.gateway import NginxGatewayActivator
from heimdall.runtime.gateway_probe import HttpRouteProbe
from heimdall.runtime.models import RuntimeDeployment
from heimdall.runtime.process import SubprocessCommandRunner
from heimdall.runtime.repository import PostgresRuntimeRepository
from heimdall.runtime.service import DockerDeploymentProcessor
from heimdall.secrets.store import FileSecretStore

CONTROL_URL = os.environ.get("HEIMDALL_TEST_CONTROL_DB_URL")
MANAGED_URL = os.environ.get("HEIMDALL_TEST_MANAGED_DB_ADMIN_URL")
DOCKER_SMOKE = os.environ.get("HEIMDALL_RUN_DOCKER_SMOKE") == "true"

pytestmark = pytest.mark.skipif(
    not CONTROL_URL or not MANAGED_URL or not DOCKER_SMOKE,
    reason="PostgreSQL and Docker runtime smoke are not enabled",
)


class FixtureGit(FakeGit):
    def __init__(self, source: Path) -> None:
        super().__init__()
        self.source = source

    def checkout_exact(self, repository_url: str, commit_sha: str, target: Path) -> None:
        assert commit_sha == "a" * 40
        shutil.copytree(self.source, target, dirs_exist_ok=True)


def test_multiservice_secret_and_database_contract_reaches_preview(tmp_path: Path) -> None:
    assert CONTROL_URL is not None
    assert MANAGED_URL is not None
    control = Database(CONTROL_URL)
    control.open()
    runner = SubprocessCommandRunner(heartbeat_interval_seconds=1)
    project_id = None
    deployment = None
    docker = None
    runtime_snapshot = None
    try:
        source = Path(__file__).parents[1] / "fixtures" / "runtime-multiservice"
        git = FixtureGit(source)
        secret_store = FileSecretStore(tmp_path / "secrets")
        projects = ProjectService(PostgresProjectRepository(control), git, secret_store)
        run_id = uuid4().hex
        project = projects.create(
            ProjectCreate(
                name=f"Multi-{run_id}",
                repositoryUrl=f"https://github.com/example/multi-{run_id}",
            )
        )
        project_id = project.id
        project = projects.update_settings(
            project.id,
            ProjectSettingsUpdate.model_validate(
                {
                    "expectedVersion": 0,
                    "services": [
                        {
                            "name": "web",
                            "build": {"context": "web", "dockerfile": "Dockerfile"},
                            "internalPort": 8080,
                            "healthPath": "/health",
                        },
                        {
                            "name": "api",
                            "build": {"context": "api", "dockerfile": "Dockerfile"},
                            "internalPort": 8000,
                            "healthPath": "/health",
                            "projectDatabaseAccess": True,
                            "environment": [
                                {"name": "APP_ENV", "kind": "PLAIN", "value": "smoke"},
                                {
                                    "name": "APP_SECRET",
                                    "kind": "SECRET",
                                    "value": "runtime-user-secret-canary",
                                },
                            ],
                        },
                    ],
                    "routes": [
                        {"path": "/api", "service": "api"},
                        {"path": "/", "service": "web"},
                    ],
                }
            ),
        )
        project_databases = ProjectDatabaseService(
            PostgresProjectDatabaseRepository(control),
            projects,
            secret_store,
            PostgresProjectDatabaseProvisioner(MANAGED_URL),
            "managed-postgres",
            5432,
        )
        assert project_databases.provision(project.id).status == "ACTIVE"

        deployments = PostgresDeploymentRepository(control)
        service = DeploymentService(deployments, projects, project_databases)
        deployment = service.request(
            project.id,
            DeploymentCreate.model_validate({"source": {"type": "MAIN_HEAD"}}),
        )
        runtime_snapshot = RuntimeDeployment.from_deployment(deployment)
        runtimes = PostgresRuntimeRepository(control)
        docker = DockerRuntime(
            runner,
            HttpHealthProbe(interval_seconds=0.1),
            managed_database_container=os.environ.get(
                "HEIMDALL_TEST_MANAGED_DB_CONTAINER", "heimdall-managed-postgres"
            ),
            command_timeout_seconds=120,
            health_timeout_seconds=20,
        )
        gateway = NginxGatewayActivator(
            runtimes,
            docker,
            runner,
            HttpRouteProbe(),
            tmp_path / "gateways",
            command_timeout_seconds=120,
        )
        processor = DockerDeploymentProcessor(
            projects,
            git,
            docker,
            gateway,
            secret_store,
            tmp_path / "workspaces",
        )
        worker = DeploymentWorker(
            deployments,
            processor,
            worker_id=f"multi-smoke-{run_id}",
            lease_duration=timedelta(seconds=30),
        )

        assert worker.run_once() is True

        completed = deployments.get(deployment.id)
        runtime = runtimes.get(project.id)
        assert completed.status is DeploymentStatus.SUCCEEDED
        assert runtime is not None
        with urlopen(f"http://127.0.0.1:{runtime.preview_port}/", timeout=3) as response:
            assert response.read() == b"web service\n"
        with urlopen(f"http://127.0.0.1:{runtime.preview_port}/api", timeout=3) as response:
            assert response.read() == b"api service\n"

        service_logs = DockerServiceLogReader(
            runner,
            secret_store,
            command_timeout_seconds=30,
        ).read(completed, "api")
        service_log_text = "\n".join(line.message for line in service_logs.lines)
        assert "[REDACTED]" in service_log_text
        assert "runtime-user-secret-canary" not in service_log_text
        assert {line.stream.value for line in service_logs.lines} == {"STDOUT", "STDERR"}

        api_container = next(name for name in runtime.active_container_names if "-api-" in name)
        inspection = runner.run(["docker", "inspect", api_container], timeout_seconds=30)
        assert "runtime-user-secret-canary" not in inspection.stdout
        assert "APP_SECRET=/run/secrets/heimdall/environment/app_secret" in inspection.stdout
        assert "DATABASE_PASSWORD_FILE=/run/secrets/heimdall/project-database-password" in (
            inspection.stdout
        )
        assert "runtime-user-secret-canary" not in json.dumps(deployment.config_snapshot)
        assert "runtime-user-secret-canary" not in json.dumps(
            [event.message for event in deployments.list_events(deployment.id)]
        )
    finally:
        if project_id is not None:
            runner.run(
                ["docker", "rm", "--force", f"hm-p{project_id.hex[:12]}-gateway"],
                timeout_seconds=30,
                check=False,
            )
        if deployment is not None and docker is not None and runtime_snapshot is not None:
            docker.cleanup_candidate(deployment, runtime_snapshot)
        control.close()
