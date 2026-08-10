from __future__ import annotations

import multiprocessing
import os
import shutil
import signal
import time
from datetime import timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
from uuid import UUID, uuid4

import pytest
from conftest import FakeGit

from heimdall.database import Database
from heimdall.deployments.models import DeploymentStatus
from heimdall.deployments.repository import PostgresDeploymentRepository
from heimdall.deployments.schemas import DeploymentCreate
from heimdall.deployments.service import DeploymentService
from heimdall.deployments.worker import DeploymentWorker
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

    def checkout_exact(self, repository_url: str, commit_sha: str, target: Path) -> None:
        assert commit_sha == "a" * 40
        shutil.copytree(self.source, target, dirs_exist_ok=True)


def _run_worker_once(
    database_url: str,
    source: str,
    runtime_root: str,
    workspace_root: str,
    gateway_root: str,
    worker_id: str,
    lease_seconds: float,
) -> None:
    database = Database(database_url)
    database.open()
    try:
        runner = SubprocessCommandRunner(heartbeat_interval_seconds=0.25)
        secrets = FileSecretStore(Path(runtime_root))
        projects = ProjectService(
            PostgresProjectRepository(database),
            FixtureGit(Path(source)),
            secrets,
        )
        deployments = PostgresDeploymentRepository(database)
        docker = DockerRuntime(
            runner,
            HttpHealthProbe(interval_seconds=0.1),
            command_timeout_seconds=120,
            health_timeout_seconds=20,
        )
        gateway = NginxGatewayActivator(
            PostgresRuntimeRepository(database),
            docker,
            runner,
            HttpRouteProbe(),
            Path(gateway_root),
            command_timeout_seconds=120,
        )
        processor = DockerDeploymentProcessor(
            projects,
            FixtureGit(Path(source)),
            docker,
            gateway,
            secrets,
            Path(workspace_root),
        )
        DeploymentWorker(
            deployments,
            processor,
            worker_id=worker_id,
            lease_duration=timedelta(seconds=lease_seconds),
        ).run_once()
    finally:
        database.close()


def _wait_for_target_header(
    runtimes: PostgresRuntimeRepository,
    project_id: UUID,
    deployment_id: UUID,
) -> int:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        runtime = runtimes.get(project_id)
        if runtime is None:
            time.sleep(0.05)
            continue
        try:
            with urlopen(f"http://127.0.0.1:{runtime.preview_port}/", timeout=0.5) as response:
                if response.headers.get("X-Heimdall-Deployment-Id") == str(deployment_id):
                    return runtime.preview_port
        except (HTTPError, URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(0.05)
    raise AssertionError("target deployment header was not observed before the deadline")


def _docker_id(runner: SubprocessCommandRunner, arguments: list[str]) -> str:
    return runner.run(
        ["docker", *arguments],
        timeout_seconds=30,
    ).stdout.strip()


def test_worker_sigkill_after_nginx_switch_is_recovered_without_rebuild(tmp_path: Path) -> None:
    assert CONTROL_URL is not None
    control = Database(CONTROL_URL)
    control.open()
    runner = SubprocessCommandRunner(heartbeat_interval_seconds=0.25)
    child: multiprocessing.Process | None = None
    project_id = None
    deployment = None
    runtime_snapshot = None
    docker = None
    try:
        source = Path(__file__).parents[1] / "fixtures" / "runtime-crash-window"
        secrets = FileSecretStore(tmp_path / "runtime")
        projects = ProjectService(PostgresProjectRepository(control), FixtureGit(source), secrets)
        run_id = uuid4().hex
        project = projects.create(
            ProjectCreate(
                name=f"Crash-{run_id}",
                repositoryUrl=f"https://github.com/example/crash-{run_id}",
            )
        )
        project_id = project.id
        projects.update_settings(
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
                    "routes": [
                        {"path": "/hold", "service": "web"},
                        {"path": "/", "service": "web"},
                    ],
                }
            ),
        )
        deployments = PostgresDeploymentRepository(control)
        deployment = DeploymentService(deployments, projects).request(
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
        context = multiprocessing.get_context("spawn")
        child = context.Process(
            target=_run_worker_once,
            args=(
                CONTROL_URL,
                str(source),
                str(tmp_path / "runtime"),
                str(tmp_path / "workspaces"),
                str(tmp_path / "gateways"),
                f"crash-worker-{run_id}",
                3,
            ),
        )
        child.start()

        preview_port = _wait_for_target_header(runtimes, project.id, deployment.id)
        before = runtimes.get(project.id)
        assert before is not None
        assert before.active_deployment_id is None
        generation = deployment.id.hex[:12]
        project_prefix = project.id.hex[:12]
        container_name = f"hm-p{project_prefix}-web-g{generation}"
        network_name = f"hm-p{project_prefix}-g{generation}"
        image_name = f"heimdall/{project.id.hex}:g{generation}-web"
        resource_ids = (
            _docker_id(runner, ["inspect", "--format", "{{.Id}}", container_name]),
            _docker_id(runner, ["network", "inspect", "--format", "{{.Id}}", network_name]),
            _docker_id(runner, ["image", "inspect", "--format", "{{.Id}}", image_name]),
        )

        os.kill(child.pid, signal.SIGKILL)
        child.join(timeout=10)
        assert child.exitcode == -signal.SIGKILL
        time.sleep(3.2)

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
            FixtureGit(source),
            docker,
            gateway,
            secrets,
            tmp_path / "workspaces",
        )
        recovery_worker = DeploymentWorker(
            deployments,
            processor,
            worker_id=f"recovery-worker-{run_id}",
            lease_duration=timedelta(seconds=30),
        )

        assert recovery_worker.run_once() is True

        completed = deployments.get(deployment.id)
        active = runtimes.get(project.id)
        assert completed.status is DeploymentStatus.SUCCEEDED
        assert active is not None
        assert active.active_deployment_id == deployment.id
        assert resource_ids == (
            _docker_id(runner, ["inspect", "--format", "{{.Id}}", container_name]),
            _docker_id(runner, ["network", "inspect", "--format", "{{.Id}}", network_name]),
            _docker_id(runner, ["image", "inspect", "--format", "{{.Id}}", image_name]),
        )
        with urlopen(f"http://127.0.0.1:{preview_port}/", timeout=3) as response:
            assert response.headers["X-Heimdall-Deployment-Id"] == str(deployment.id)
            assert response.read() == b"crash window runtime\n"
    finally:
        if child is not None and child.is_alive():
            child.kill()
            child.join(timeout=10)
        if project_id is not None:
            runner.run(
                ["docker", "rm", "--force", f"hm-p{project_id.hex[:12]}-gateway"],
                timeout_seconds=30,
                check=False,
            )
        if deployment is not None and runtime_snapshot is not None and docker is not None:
            docker.cleanup_candidate(deployment, runtime_snapshot)
        control.close()
