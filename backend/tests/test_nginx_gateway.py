from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_runtime_models import runtime_deployment

from heimdall.deployments.worker import RuntimeFailure
from heimdall.runtime.docker import CandidateGeneration, RunningService
from heimdall.runtime.gateway import NginxGatewayActivator
from heimdall.runtime.models import RuntimeDeployment
from heimdall.runtime.process import CommandResult
from heimdall.runtime.repository import ProjectRuntime


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


class GatewayRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(
        self,
        arguments,
        *,
        timeout_seconds,
        heartbeat=None,
        check=True,
    ) -> CommandResult:
        command = list(arguments)
        self.calls.append(command)
        if heartbeat is not None:
            heartbeat()
        if command[1] == "inspect":
            return CommandResult(1, "")
        if command[1] == "port":
            return CommandResult(0, "127.0.0.1:48080\n")
        return CommandResult(0, "")


class ConflictingGatewayRunner(GatewayRunner):
    def run(
        self,
        arguments,
        *,
        timeout_seconds,
        heartbeat=None,
        check=True,
    ) -> CommandResult:
        command = list(arguments)
        if command[1] == "inspect":
            self.calls.append(command)
            return CommandResult(0, json.dumps({"owner": "external"}))
        return super().run(
            command,
            timeout_seconds=timeout_seconds,
            heartbeat=heartbeat,
            check=check,
        )


class GatewayDocker:
    def __init__(self) -> None:
        self.promoted: CandidateGeneration | None = None
        self.retired: list[tuple] = []

    def promote_candidate(self, candidate: CandidateGeneration) -> None:
        self.promoted = candidate

    def retire_generation(self, network_name, container_names, image_names, deployment_id) -> None:
        self.retired.append((network_name, container_names, image_names, deployment_id))


class HealthyRoute:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def probe(self, url, *, timeout_seconds, heartbeat) -> None:
        self.urls.append(url)
        heartbeat()


class FailedRoute(HealthyRoute):
    def probe(self, url, *, timeout_seconds, heartbeat) -> None:
        raise RuntimeFailure("ACTIVATION", "GATEWAY_ROUTE_PROBE_FAILED")


class Progress:
    def __init__(self) -> None:
        self.heartbeats = 0

    def heartbeat(self) -> None:
        self.heartbeats += 1


def candidate() -> CandidateGeneration:
    return CandidateGeneration(
        network_name="hm-project-generation",
        services=(
            RunningService(
                name="api",
                container_name="hm-api-generation",
                image_name="heimdall/project:api",
                health_port=49152,
            ),
        ),
    )


def test_gateway_activates_candidate_and_persists_stable_preview(tmp_path: Path) -> None:
    item = runtime_deployment()
    runtime = RuntimeDeployment.from_deployment(item)
    repository = MemoryRuntimes()
    docker = GatewayDocker()
    runner = GatewayRunner()
    route = HealthyRoute()
    activator = NginxGatewayActivator(repository, docker, runner, route, tmp_path / "gateways")

    activator.activate(item, runtime, candidate(), Progress())

    assert repository.item is not None
    assert repository.item.active_deployment_id == item.id
    assert repository.item.preview_port == 48080
    assert docker.promoted == candidate()
    assert route.urls == ["http://127.0.0.1:48080/"]
    config = (tmp_path / "gateways" / item.project_id.hex / "current.conf").read_text()
    assert f"api-g-{item.id.hex[:12]}" in config
    assert f"# deployment: {item.id}" in config


def test_failed_route_probe_restores_last_known_good_config(tmp_path: Path) -> None:
    item = runtime_deployment()
    runtime = RuntimeDeployment.from_deployment(item)
    repository = MemoryRuntimes()
    docker = GatewayDocker()
    runner = GatewayRunner()
    activator = NginxGatewayActivator(
        repository, docker, runner, FailedRoute(), tmp_path / "gateways"
    )

    with pytest.raises(RuntimeFailure):
        activator.activate(item, runtime, candidate(), Progress())
    activator.rollback_candidate(item)

    assert repository.item is not None
    assert repository.item.active_deployment_id is None
    assert docker.promoted is None
    config = (tmp_path / "gateways" / item.project_id.hex / "current.conf").read_text()
    assert config == "server { listen 8080; location / { return 503; } }\n"
    gateway_name = f"hm-p{item.project_id.hex[:12]}-gateway"
    reloads = [call for call in runner.calls if call[1:3] == ["exec", gateway_name]]
    assert len(reloads) == 2
    disconnects = [
        call for call in runner.calls if call[1:4] == ["network", "disconnect", "--force"]
    ]
    assert len(disconnects) == 1


def test_unmanaged_gateway_name_collision_is_rejected(tmp_path: Path) -> None:
    item = runtime_deployment()
    runtime = RuntimeDeployment.from_deployment(item)
    repository = MemoryRuntimes()
    docker = GatewayDocker()
    runner = ConflictingGatewayRunner()
    activator = NginxGatewayActivator(
        repository, docker, runner, HealthyRoute(), tmp_path / "gateways"
    )

    with pytest.raises(RuntimeFailure) as raised:
        activator.activate(item, runtime, candidate(), Progress())

    assert raised.value.code == "GATEWAY_NAME_CONFLICT"
    assert repository.item is None
    assert docker.promoted is None
