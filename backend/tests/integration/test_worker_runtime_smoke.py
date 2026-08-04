from __future__ import annotations

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
from heimdall.deployments.worker import DeploymentWorker, RuntimeFailure
from heimdall.projects.repository import PostgresProjectRepository
from heimdall.projects.schemas import ProjectCreate, ProjectSettingsUpdate
from heimdall.projects.service import ProjectService
from heimdall.runtime.docker import DockerRuntime, HttpHealthProbe
from heimdall.runtime.gateway import HttpRouteProbe, NginxGatewayActivator
from heimdall.runtime.models import RuntimeDeployment
from heimdall.runtime.process import SubprocessCommandRunner
from heimdall.runtime.repository import PostgresRuntimeRepository
from heimdall.runtime.service import DockerDeploymentProcessor
from heimdall.secrets.store import FileSecretStore

CONTROL_URL = os.environ.get("HEIMDALL_TEST_CONTROL_DB_URL")
DOCKER_SMOKE = os.environ.get("HEIMDALL_RUN_DOCKER_SMOKE") == "true"

pytestmark = pytest.mark.skipif(
    not CONTROL_URL or not DOCKER_SMOKE,
    reason="Control PostgreSQL and Docker runtime smoke are not enabled",
)


class FixtureGit(FakeGit):
    def __init__(self, source: Path) -> None:
        super().__init__()
        self.source = source
        self.checked_out_sha: str | None = None

    def checkout_exact(self, repository_url: str, commit_sha: str, target: Path) -> None:
        assert repository_url.startswith("https://github.com/")
        assert commit_sha == "a" * 40
        self.checked_out_sha = commit_sha
        shutil.copytree(self.source, target, dirs_exist_ok=True)


class RejectingRouteProbe:
    def probe(self, url, *, timeout_seconds, heartbeat) -> None:
        heartbeat()
        raise RuntimeFailure("ACTIVATION", "GATEWAY_ROUTE_PROBE_FAILED")


def test_worker_claim_to_stable_preview_succeeds_end_to_end(tmp_path: Path) -> None:
    assert CONTROL_URL is not None
    control = Database(CONTROL_URL)
    control.open()
    runner = SubprocessCommandRunner(heartbeat_interval_seconds=1)
    project_id = None
    deployment = None
    docker = None
    runtime_snapshot = None
    try:
        source = Path(__file__).parents[1] / "fixtures" / "runtime-single"
        git = FixtureGit(source)
        secret_store = FileSecretStore(tmp_path / "secrets")
        projects = ProjectService(PostgresProjectRepository(control), git, secret_store)
        run_id = uuid4().hex
        project = projects.create(
            ProjectCreate(
                name=f"Runtime-{run_id}",
                repositoryUrl=f"https://github.com/example/runtime-{run_id}",
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
                            "build": {"context": ".", "dockerfile": "Dockerfile"},
                            "internalPort": 8080,
                            "healthPath": "/health",
                        }
                    ],
                    "routes": [{"path": "/", "service": "web"}],
                }
            ),
        )
        deployments = PostgresDeploymentRepository(control)
        service = DeploymentService(deployments, projects)
        deployment = service.request(
            project.id,
            DeploymentCreate.model_validate({"source": {"type": "MAIN_HEAD"}}),
        )
        runtime_snapshot = RuntimeDeployment.from_deployment(deployment)
        runtimes = PostgresRuntimeRepository(control)
        docker = DockerRuntime(
            runner,
            HttpHealthProbe(interval_seconds=0.1),
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
            worker_id=f"runtime-smoke-{run_id}",
            lease_duration=timedelta(seconds=30),
        )

        assert worker.run_once() is True

        completed = deployments.get(deployment.id)
        runtime = runtimes.get(project.id)
        assert completed.status is DeploymentStatus.SUCCEEDED
        assert runtime is not None
        assert runtime.active_deployment_id == deployment.id
        assert git.checked_out_sha == deployment.resolved_commit_sha
        with urlopen(f"http://127.0.0.1:{runtime.preview_port}/", timeout=3) as response:
            assert response.read() == b"heimdall runtime smoke\n"
        assert [event.stage for event in deployments.list_events(deployment.id)] == [
            "PREPARING",
            "BUILDING",
            "STARTING",
            "HEALTH_CHECKING",
            "ACTIVATING",
            "SUCCEEDED",
        ]

        failed_deployment = service.request(
            project.id,
            DeploymentCreate.model_validate({"source": {"type": "MAIN_HEAD"}}),
        )
        rejecting_gateway = NginxGatewayActivator(
            runtimes,
            docker,
            runner,
            RejectingRouteProbe(),
            tmp_path / "gateways",
            command_timeout_seconds=120,
        )
        rejecting_processor = DockerDeploymentProcessor(
            projects,
            git,
            docker,
            rejecting_gateway,
            secret_store,
            tmp_path / "workspaces",
        )
        rejecting_worker = DeploymentWorker(
            deployments,
            rejecting_processor,
            worker_id=f"runtime-reject-{run_id}",
            lease_duration=timedelta(seconds=30),
        )

        assert rejecting_worker.run_once() is True

        failed = deployments.get(failed_deployment.id)
        preserved = runtimes.get(project.id)
        assert failed.status is DeploymentStatus.FAILED
        assert failed.failure_stage == "ACTIVATION"
        assert failed.failure_code == "GATEWAY_ROUTE_PROBE_FAILED"
        assert preserved is not None
        assert preserved.active_deployment_id == deployment.id
        assert preserved.preview_port == runtime.preview_port
        with urlopen(f"http://127.0.0.1:{preserved.preview_port}/", timeout=3) as response:
            assert response.read() == b"heimdall runtime smoke\n"
    finally:
        if project_id is not None:
            gateway_name = f"hm-p{project_id.hex[:12]}-gateway"
            runner.run(
                ["docker", "rm", "--force", gateway_name],
                timeout_seconds=30,
                check=False,
            )
        if deployment is not None and docker is not None and runtime_snapshot is not None:
            docker.cleanup_candidate(deployment, runtime_snapshot)
        control.close()
