from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen
from uuid import uuid4

import pytest

from heimdall.deployments.models import Deployment, DeploymentSource, DeploymentStatus
from heimdall.deployments.worker import RecoveryDisposition
from heimdall.runtime.docker import DockerRuntime, DockerServiceLogReader, HttpHealthProbe
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


def test_service_log_snapshot_redacts_known_secret_and_separates_streams(
    tmp_path: Path,
) -> None:
    project_id = uuid4()
    deployment_id = uuid4()
    now = datetime.now(UTC)
    secret_store = FileSecretStore(tmp_path / "secrets")
    stored = secret_store.create(
        f"projects/{project_id}/environment/logs/log_secret",
        1,
        "service-log-secret-canary",
    )
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
                    "name": "logs",
                    "build": {"context": ".", "dockerfile": "Dockerfile"},
                    "internalPort": 8080,
                    "healthPath": "/health",
                    "environment": [
                        {
                            "name": "LOG_SECRET",
                            "kind": "SECRET",
                            "secretReference": stored.reference,
                            "secretVersion": stored.version,
                            "secretFingerprint": stored.fingerprint,
                        }
                    ],
                    "projectDatabaseAccess": False,
                }
            ],
            "routes": [{"path": "/", "service": "logs"}],
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
    source = Path(__file__).parents[1] / "fixtures" / "runtime-service-logs"
    candidate = None
    try:
        candidate = docker.start_candidate(deployment, runtime, source, secret_store, Progress())

        snapshot = DockerServiceLogReader(
            runner,
            secret_store,
            command_timeout_seconds=30,
        ).read(deployment, None)

        messages = "\n".join(line.message for line in snapshot.lines)
        assert messages.count("[REDACTED]") == 2
        assert "service-log-secret-canary" not in messages
        assert {line.stream.value for line in snapshot.lines} == {"STDOUT", "STDERR"}
    finally:
        docker.cleanup_candidate(deployment, runtime)
    assert candidate is not None
    assert (
        runner.run(
            ["docker", "network", "inspect", candidate.network_name],
            timeout_seconds=30,
            check=False,
        ).returncode
        != 0
    )
    for service in candidate.services:
        assert (
            runner.run(
                ["docker", "inspect", service.container_name],
                timeout_seconds=30,
                check=False,
            ).returncode
            != 0
        )
        assert (
            runner.run(
                ["docker", "image", "inspect", service.image_name],
                timeout_seconds=30,
                check=False,
            ).returncode
            != 0
        )


def test_single_service_candidate_and_stopped_gateway_recovery(tmp_path: Path) -> None:
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
    cleanup_candidate = None
    cleanup_deployment = None
    cleanup_runtime = None
    replacement_deployment = None
    replacement_runtime = None
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
            assert response.headers["X-Heimdall-Deployment-Id"] == str(deployment.id)
            assert response.read() == b"heimdall runtime smoke\n"

        active = runtimes.item
        runtimes.item = ProjectRuntime(
            project_id=active.project_id,
            gateway_container_name=active.gateway_container_name,
            preview_port=active.preview_port,
            active_deployment_id=None,
            active_network_name=None,
            active_container_names=(),
            active_image_names=(),
            updated_at=datetime.now(UTC),
        )

        assert gateway.recover(deployment, runtime, Progress()) is RecoveryDisposition.ACTIVE
        assert runtimes.item.active_deployment_id == deployment.id

        stable_port = runtimes.item.preview_port
        gateway_name = runtimes.item.gateway_container_name
        runner.run(
            ["docker", "stop", gateway_name],
            timeout_seconds=30,
        )
        replacement_deployment = replace(deployment, id=uuid4())
        replacement_runtime = RuntimeDeployment.from_deployment(replacement_deployment)
        replacement_candidate = docker.start_candidate(
            replacement_deployment,
            replacement_runtime,
            source,
            FileSecretStore(tmp_path / "replacement-secrets"),
            Progress(),
        )

        gateway.activate(
            replacement_deployment,
            replacement_runtime,
            replacement_candidate,
            Progress(),
        )

        assert runtimes.item.active_deployment_id == replacement_deployment.id
        assert runtimes.item.preview_port == stable_port
        assert (
            runner.run(
                ["docker", "inspect", "--format", "{{json .State.Running}}", gateway_name],
                timeout_seconds=30,
            ).stdout.strip()
            == "true"
        )
        HttpRouteProbe().probe(
            f"http://127.0.0.1:{stable_port}/",
            timeout_seconds=10,
            heartbeat=Progress().heartbeat,
        )
        with urlopen(f"http://127.0.0.1:{stable_port}/", timeout=3) as response:
            assert response.headers["X-Heimdall-Deployment-Id"] == str(replacement_deployment.id)
            assert response.read() == b"heimdall runtime smoke\n"

        cleanup_deployment = replace(deployment, id=uuid4())
        cleanup_runtime = RuntimeDeployment.from_deployment(cleanup_deployment)
        cleanup_candidate = docker.start_candidate(
            cleanup_deployment,
            cleanup_runtime,
            source,
            FileSecretStore(tmp_path / "cleanup-secrets"),
            Progress(),
        )
        docker.cleanup_candidate_verified(cleanup_deployment, cleanup_runtime, Progress())
        assert (
            runner.run(
                ["docker", "network", "inspect", cleanup_candidate.network_name],
                timeout_seconds=30,
                check=False,
            ).returncode
            != 0
        )
        for service in cleanup_candidate.services:
            assert (
                runner.run(
                    ["docker", "inspect", service.container_name],
                    timeout_seconds=30,
                    check=False,
                ).returncode
                != 0
            )
            assert (
                runner.run(
                    ["docker", "image", "inspect", service.image_name],
                    timeout_seconds=30,
                    check=False,
                ).returncode
                != 0
            )
        with urlopen(
            f"http://127.0.0.1:{runtimes.item.preview_port}/",
            timeout=3,
        ) as response:
            assert response.headers["X-Heimdall-Deployment-Id"] == str(replacement_deployment.id)
            assert response.read() == b"heimdall runtime smoke\n"
    finally:
        if cleanup_deployment is not None and cleanup_runtime is not None:
            docker.cleanup_candidate(cleanup_deployment, cleanup_runtime)
        gateway_name = f"hm-p{project_id.hex[:12]}-gateway"
        runner.run(
            ["docker", "rm", "--force", gateway_name],
            timeout_seconds=30,
            check=False,
        )
        if replacement_deployment is not None and replacement_runtime is not None:
            docker.cleanup_candidate(replacement_deployment, replacement_runtime)
        docker.cleanup_candidate(deployment, runtime)
