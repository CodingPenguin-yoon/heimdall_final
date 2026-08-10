from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from test_runtime_models import runtime_deployment

from heimdall.deployments.worker import RecoveryDisposition, RuntimeFailure
from heimdall.runtime.docker import CandidateGeneration, RunningService
from heimdall.runtime.gateway import GatewayObservation, HttpRouteProbe, NginxGatewayActivator
from heimdall.runtime.models import RuntimeDeployment
from heimdall.runtime.process import CommandExecutionError, CommandResult
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


class ExistingGatewayRunner(GatewayRunner):
    def __init__(self, project_id) -> None:
        super().__init__()
        self.project_id = project_id

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
            labels = {
                "heimdall.managed": "true",
                "heimdall.project-id": str(self.project_id),
                "heimdall.kind": "gateway",
            }
            output = (
                {"labels": labels, "running": True} if ".State.Running" in command[3] else labels
            )
            return CommandResult(
                0,
                json.dumps(output),
            )
        return super().run(
            command,
            timeout_seconds=timeout_seconds,
            heartbeat=heartbeat,
            check=check,
        )


class StoppedGatewayRunner(ExistingGatewayRunner):
    def __init__(self, project_id, *, fail_detached_run: int | None = None) -> None:
        super().__init__(project_id)
        self.gateway_exists = True
        self.gateway_running = False
        self.detached_runs = 0
        self.fail_detached_run = fail_detached_run

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
            if not self.gateway_exists:
                return CommandResult(1, "")
            labels = {
                "heimdall.managed": "true",
                "heimdall.project-id": str(self.project_id),
                "heimdall.kind": "gateway",
            }
            output = (
                {"labels": labels, "running": self.gateway_running}
                if ".State.Running" in command[3]
                else labels
            )
            return CommandResult(
                0,
                json.dumps(output),
            )
        if command[1] == "rm":
            self.calls.append(command)
            if heartbeat is not None:
                heartbeat()
            self.gateway_exists = False
            self.gateway_running = False
            return CommandResult(0, "")
        if command[1:3] == ["run", "--detach"]:
            self.calls.append(command)
            if heartbeat is not None:
                heartbeat()
            self.detached_runs += 1
            if self.detached_runs == self.fail_detached_run:
                raise CommandExecutionError(1)
            self.gateway_exists = True
            self.gateway_running = True
            return CommandResult(0, "")
        return super().run(
            command,
            timeout_seconds=timeout_seconds,
            heartbeat=heartbeat,
            check=check,
        )


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
    def __init__(self, observed: CandidateGeneration | None = None) -> None:
        self.promoted: CandidateGeneration | None = None
        self.retired: list[tuple] = []
        self.observed = observed
        self.verified: CandidateGeneration | None = None

    def observe_candidate(self, deployment, runtime, progress) -> CandidateGeneration | None:
        progress.heartbeat()
        return self.observed

    def verify_candidate(self, runtime, candidate, progress) -> None:
        self.verified = candidate
        progress.heartbeat()

    def promote_candidate(self, candidate: CandidateGeneration) -> None:
        self.promoted = candidate

    def retire_generation(self, network_name, container_names, image_names, deployment_id) -> None:
        self.retired.append((network_name, container_names, image_names, deployment_id))


class HealthyRoute:
    def __init__(self, observed_deployment_id=None, *, reachable: bool = True) -> None:
        self.urls: list[str] = []
        self.observed_deployment_id = observed_deployment_id
        self.reachable = reachable

    def probe(self, url, *, timeout_seconds, heartbeat) -> None:
        self.urls.append(url)
        heartbeat()

    def observe(self, url, *, timeout_seconds, heartbeat) -> GatewayObservation:
        heartbeat()
        return GatewayObservation(self.reachable, self.observed_deployment_id)


class FailedRoute(HealthyRoute):
    def probe(self, url, *, timeout_seconds, heartbeat) -> None:
        raise RuntimeFailure("ACTIVATION", "GATEWAY_ROUTE_PROBE_FAILED")


class SequencedRoute(HealthyRoute):
    def __init__(self, observations) -> None:
        super().__init__()
        self.observations = list(observations)

    def observe(self, url, *, timeout_seconds, heartbeat) -> GatewayObservation:
        heartbeat()
        return GatewayObservation(True, self.observations.pop(0))


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


def test_route_observer_reads_the_nginx_deployment_marker(monkeypatch) -> None:
    deployment_id = uuid4()

    class Response:
        def __init__(self) -> None:
            self.headers = {"X-Heimdall-Deployment-Id": str(deployment_id)}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    monkeypatch.setattr("heimdall.runtime.gateway.urlopen", lambda request, timeout: Response())

    observation = HttpRouteProbe().observe(
        "http://127.0.0.1:48080/",
        timeout_seconds=1,
        heartbeat=lambda: None,
    )

    assert observation == GatewayObservation(True, deployment_id)


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
    assert f'add_header X-Heimdall-Deployment-Id "{item.id}" always;' in config


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
    assert 'add_header X-Heimdall-Deployment-Id "none" always;' in config
    assert "return 503" in config
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


def test_activation_recreates_stopped_managed_gateway_on_stored_port_and_network(
    tmp_path: Path,
) -> None:
    item = runtime_deployment()
    runtime = RuntimeDeployment.from_deployment(item)
    gateway_name = f"hm-p{item.project_id.hex[:12]}-gateway"
    repository = MemoryRuntimes()
    repository.item = ProjectRuntime(
        project_id=item.project_id,
        gateway_container_name=gateway_name,
        preview_port=48080,
        active_deployment_id=uuid4(),
        active_network_name="previous-network",
        active_container_names=("previous-container",),
        active_image_names=("previous-image",),
        updated_at=datetime.now(UTC),
    )
    runner = StoppedGatewayRunner(item.project_id)
    route = HealthyRoute()
    activator = NginxGatewayActivator(
        repository,
        GatewayDocker(),
        runner,
        route,
        tmp_path / "gateways",
    )

    activator.activate(item, runtime, candidate(), Progress())

    assert repository.item is not None
    assert repository.item.preview_port == 48080
    assert ["docker", "rm", gateway_name] in runner.calls
    assert ["docker", "rm", "--force", gateway_name] in runner.calls
    create_calls = [call for call in runner.calls if call[1:3] == ["run", "--detach"]]
    assert [call[call.index("--network") + 1] for call in create_calls] == [
        "previous-network",
        candidate().network_name,
    ]
    assert [call[call.index("--publish") + 1] for call in create_calls] == [
        "127.0.0.1:48080:8080",
        "127.0.0.1:48080:8080",
    ]
    assert route.urls == ["http://127.0.0.1:48080/", "http://127.0.0.1:48080/"]


def test_failed_candidate_network_rebase_restores_previous_gateway(tmp_path: Path) -> None:
    item = runtime_deployment()
    runtime = RuntimeDeployment.from_deployment(item)
    previous_id = uuid4()
    gateway_name = f"hm-p{item.project_id.hex[:12]}-gateway"
    repository = MemoryRuntimes()
    repository.item = ProjectRuntime(
        project_id=item.project_id,
        gateway_container_name=gateway_name,
        preview_port=48080,
        active_deployment_id=previous_id,
        active_network_name="previous-network",
        active_container_names=("previous-container",),
        active_image_names=("previous-image",),
        updated_at=datetime.now(UTC),
    )
    runner = StoppedGatewayRunner(item.project_id, fail_detached_run=2)
    docker = GatewayDocker()
    activator = NginxGatewayActivator(
        repository,
        docker,
        runner,
        HealthyRoute(),
        tmp_path / "gateways",
    )

    with pytest.raises(RuntimeFailure) as raised:
        activator.activate(item, runtime, candidate(), Progress())

    assert raised.value.code == "GATEWAY_START_FAILED"
    assert repository.item is not None
    assert repository.item.active_deployment_id == previous_id
    assert runner.gateway_exists is True
    assert runner.gateway_running is True
    create_calls = [call for call in runner.calls if call[1:3] == ["run", "--detach"]]
    assert [call[call.index("--network") + 1] for call in create_calls] == [
        "previous-network",
        candidate().network_name,
        "previous-network",
    ]
    current = (tmp_path / "gateways" / item.project_id.hex / "current.conf").read_text()
    assert 'X-Heimdall-Deployment-Id "none"' in current
    assert docker.promoted is None
    assert docker.retired == []


def test_activation_reuses_running_managed_gateway(tmp_path: Path) -> None:
    item = runtime_deployment()
    runtime = RuntimeDeployment.from_deployment(item)
    gateway_name = f"hm-p{item.project_id.hex[:12]}-gateway"
    repository = MemoryRuntimes()
    repository.item = ProjectRuntime(
        project_id=item.project_id,
        gateway_container_name=gateway_name,
        preview_port=48080,
        active_deployment_id=uuid4(),
        active_network_name="previous-network",
        active_container_names=("previous-container",),
        active_image_names=("previous-image",),
        updated_at=datetime.now(UTC),
    )
    runner = ExistingGatewayRunner(item.project_id)
    activator = NginxGatewayActivator(
        repository,
        GatewayDocker(),
        runner,
        HealthyRoute(),
        tmp_path / "gateways",
    )

    activator.activate(item, runtime, candidate(), Progress())

    assert not any(call[1] == "rm" for call in runner.calls)
    assert not any(call[1:4] == ["run", "--detach", "--name"] for call in runner.calls)


def test_recovery_finalizes_the_generation_served_by_nginx(tmp_path: Path) -> None:
    item = runtime_deployment()
    runtime = RuntimeDeployment.from_deployment(item)
    previous_id = uuid4()
    repository = MemoryRuntimes()
    repository.item = ProjectRuntime(
        project_id=item.project_id,
        gateway_container_name=f"hm-p{item.project_id.hex[:12]}-gateway",
        preview_port=48080,
        active_deployment_id=previous_id,
        active_network_name="previous-network",
        active_container_names=("previous-container",),
        active_image_names=("previous-image",),
        updated_at=datetime.now(UTC),
    )
    directory = tmp_path / "gateways" / item.project_id.hex
    directory.mkdir(parents=True)
    current = f"# deployment: {item.id}\nserver {{ listen 8080; }}\n"
    (directory / "current.conf").write_text(current)
    (directory / "last-good.config").write_text(
        f"# deployment: {previous_id}\nserver {{ listen 8080; }}\n"
    )
    observed_candidate = candidate()
    docker = GatewayDocker(observed_candidate)
    runner = ExistingGatewayRunner(item.project_id)
    route = HealthyRoute(item.id)
    activator = NginxGatewayActivator(repository, docker, runner, route, tmp_path / "gateways")

    disposition = activator.recover(item, runtime, Progress())

    assert disposition is RecoveryDisposition.ACTIVE
    assert repository.item is not None
    assert repository.item.active_deployment_id == item.id
    assert docker.promoted == observed_candidate
    assert docker.verified == observed_candidate
    assert docker.retired == [
        (
            "previous-network",
            ("previous-container",),
            ("previous-image",),
            previous_id,
        )
    ]


def test_recovery_restores_target_file_when_nginx_still_serves_previous(
    tmp_path: Path,
) -> None:
    item = runtime_deployment()
    runtime = RuntimeDeployment.from_deployment(item)
    previous_id = uuid4()
    repository = MemoryRuntimes()
    repository.item = ProjectRuntime(
        project_id=item.project_id,
        gateway_container_name=f"hm-p{item.project_id.hex[:12]}-gateway",
        preview_port=48080,
        active_deployment_id=previous_id,
        active_network_name="previous-network",
        active_container_names=("previous-container",),
        active_image_names=("previous-image",),
        updated_at=datetime.now(UTC),
    )
    directory = tmp_path / "gateways" / item.project_id.hex
    directory.mkdir(parents=True)
    (directory / "current.conf").write_text(f"# deployment: {item.id}\nserver {{ listen 8080; }}\n")
    previous_config = f"# deployment: {previous_id}\nserver {{ listen 8080; }}\n"
    (directory / "last-good.config").write_text(previous_config)
    runner = ExistingGatewayRunner(item.project_id)
    activator = NginxGatewayActivator(
        repository,
        GatewayDocker(),
        runner,
        HealthyRoute(previous_id),
        tmp_path / "gateways",
    )

    disposition = activator.recover(item, runtime, Progress())

    assert disposition is RecoveryDisposition.SAFE_TO_RETRY
    assert (directory / "current.conf").read_text() == previous_config
    assert repository.item is not None
    assert any(
        call[1:3] == ["exec", repository.item.gateway_container_name] for call in runner.calls
    )


def test_recovery_preserves_candidate_when_gateway_cannot_be_observed(tmp_path: Path) -> None:
    item = runtime_deployment()
    runtime = RuntimeDeployment.from_deployment(item)
    repository = MemoryRuntimes()
    repository.item = ProjectRuntime(
        project_id=item.project_id,
        gateway_container_name=f"hm-p{item.project_id.hex[:12]}-gateway",
        preview_port=48080,
        active_deployment_id=None,
        active_network_name=None,
        active_container_names=(),
        active_image_names=(),
        updated_at=datetime.now(UTC),
    )
    activator = NginxGatewayActivator(
        repository,
        GatewayDocker(candidate()),
        ExistingGatewayRunner(item.project_id),
        HealthyRoute(reachable=False),
        tmp_path / "gateways",
    )

    disposition = activator.recover(item, runtime, Progress())

    assert disposition is RecoveryDisposition.UNCERTAIN


def test_recovery_rolls_back_when_served_target_candidate_is_incomplete(tmp_path: Path) -> None:
    item = runtime_deployment()
    runtime = RuntimeDeployment.from_deployment(item)
    previous_id = uuid4()
    repository = MemoryRuntimes()
    repository.item = ProjectRuntime(
        project_id=item.project_id,
        gateway_container_name=f"hm-p{item.project_id.hex[:12]}-gateway",
        preview_port=48080,
        active_deployment_id=previous_id,
        active_network_name="previous-network",
        active_container_names=("previous-container",),
        active_image_names=("previous-image",),
        updated_at=datetime.now(UTC),
    )
    directory = tmp_path / "gateways" / item.project_id.hex
    directory.mkdir(parents=True)
    (directory / "current.conf").write_text(f"# deployment: {item.id}\nserver {{ listen 8080; }}\n")
    previous_config = f"# deployment: {previous_id}\nserver {{ listen 8080; }}\n"
    (directory / "last-good.config").write_text(previous_config)
    activator = NginxGatewayActivator(
        repository,
        GatewayDocker(observed=None),
        ExistingGatewayRunner(item.project_id),
        SequencedRoute([item.id, previous_id]),
        tmp_path / "gateways",
    )

    disposition = activator.recover(item, runtime, Progress())

    assert disposition is RecoveryDisposition.SAFE_TO_RETRY
    assert (directory / "current.conf").read_text() == previous_config
