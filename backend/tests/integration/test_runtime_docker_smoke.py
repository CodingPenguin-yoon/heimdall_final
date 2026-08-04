from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen
from uuid import uuid4

import pytest

from heimdall.deployments.models import Deployment, DeploymentSource, DeploymentStatus
from heimdall.runtime.docker import DockerRuntime, HttpHealthProbe
from heimdall.runtime.gateway import HttpRouteProbe, NginxGatewayActivator
from heimdall.runtime.models import RuntimeDeployment
from heimdall.runtime.process import SubprocessCommandRunner
from heimdall.runtime.repository import ProjectRuntime
from heimdall.secrets.store import FileSecretStore

pytestmark = pytest.mark.skipif(
    os.environ.get("HEIMDALL_RUN_DOCKER_SMOKE") != "true",
    reason="Docker runtime smoke is not enabled",
)


class MemoryRuntimes:
    def __init__(self) -> None:
        self.item: ProjectRuntime | None = None

    def get(self, project_id):
        return self.item if self.item is not None and self.item.project_id == project_id else None

    def ensure_gateway(self, project_id, gateway_container_name, preview_port):
        if self.item is None:
            self.item = ProjectRuntime(
                project_id=project_id,
                gateway_container_name=gateway_container_name,
                preview_port=preview_port,
                active_deployment_id=None,
                active_network_name=None,
                active_container_names=(),
                active_image_names=(),
                updated_at=datetime.now(UTC),
            )
        return self.item

    def activate(
        self,
        project_id,
        deployment_id,
        network_name,
        container_names,
        image_names,
    ):
        previous = self.item
        assert previous is not None
        self.item = ProjectRuntime(
            project_id=project_id,
            gateway_container_name=previous.gateway_container_name,
            preview_port=previous.preview_port,
            active_deployment_id=deployment_id,
            active_network_name=network_name,
            active_container_names=container_names,
            active_image_names=image_names,
            updated_at=datetime.now(UTC),
        )
        return previous


class Progress:
    def heartbeat(self) -> None:
        return

    def stage(self, status, code, message) -> None:
        assert status in {
            DeploymentStatus.BUILDING,
            DeploymentStatus.STARTING,
            DeploymentStatus.HEALTH_CHECKING,
        }
        assert code and message


def test_single_service_candidate_is_activated_behind_stable_gateway(tmp_path: Path) -> None:
    project_id = uuid4()
    deployment_id = uuid4()
    now = datetime.now(UTC)
    deployment = Deployment(
        id=deployment_id,
        project_id=project_id,
        source_type=DeploymentSource.MAIN_HEAD,
        requested_commit_sha=None,
        resolved_commit_sha="a" * 40,
        config_version=1,
        config_snapshot={
            "services": [
                {
                    "name": "web",
                    "build": {"context": ".", "dockerfile": "Dockerfile"},
                    "internalPort": 8080,
                    "healthPath": "/health",
                    "environment": [],
                    "projectDatabaseAccess": False,
                }
            ],
            "routes": [{"path": "/", "service": "web"}],
        },
        status=DeploymentStatus.PREPARING,
        failure_stage=None,
        failure_code=None,
        created_at=now,
        updated_at=now,
        terminal_at=None,
    )
    runtime = RuntimeDeployment.from_deployment(deployment)
    runner = SubprocessCommandRunner(heartbeat_interval_seconds=1)
    docker = DockerRuntime(
        runner,
        HttpHealthProbe(interval_seconds=0.1),
        command_timeout_seconds=120,
        health_timeout_seconds=20,
    )
    runtimes = MemoryRuntimes()
    gateway = NginxGatewayActivator(
        runtimes,
        docker,
        runner,
        HttpRouteProbe(),
        tmp_path / "gateways",
        command_timeout_seconds=120,
    )
    source = Path(__file__).parents[1] / "fixtures" / "runtime-single"
    candidate = None
    try:
        candidate = docker.start_candidate(
            deployment,
            runtime,
            source,
            FileSecretStore(tmp_path / "secrets"),
            Progress(),
        )
        gateway.activate(deployment, runtime, candidate, Progress())

        assert runtimes.item is not None
        assert runtimes.item.active_deployment_id == deployment.id
        with urlopen(
            f"http://127.0.0.1:{runtimes.item.preview_port}/",
            timeout=3,
        ) as response:
            assert response.read() == b"heimdall runtime smoke\n"
    finally:
        gateway_name = f"hm-p{project_id.hex[:12]}-gateway"
        runner.run(
            ["docker", "rm", "--force", gateway_name],
            timeout_seconds=30,
            check=False,
        )
        docker.cleanup_candidate(deployment, runtime)
