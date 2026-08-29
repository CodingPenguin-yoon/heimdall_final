from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from test_runtime_models import runtime_deployment

from heimdall.deployments.worker import RecoveryDisposition, RuntimeFailure
from heimdall.runtime.docker import CandidateGeneration, RunningService
from heimdall.runtime.edge_network import DockerEdgeNetworkConnector
from heimdall.runtime.gateway import NginxGatewayActivator
from heimdall.runtime.gateway_identity import project_gateway_alias, project_gateway_name
from heimdall.runtime.gateway_probe import GatewayObservation, HttpRouteProbe
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


class RollbackScopeRunner(GatewayRunner):
    def __init__(
        self,
        project_id,
        deployment_id,
        *,
        gateway_state: str = "managed",
        network_state: str = "managed",
    ) -> None:
        super().__init__()
        self.project_id = project_id
        self.deployment_id = deployment_id
        self.gateway_state = gateway_state
        self.network_state = network_state

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
            if heartbeat is not None:
                heartbeat()
            if self.gateway_state == "missing":
                return CommandResult(1, "")
            if self.gateway_state == "uncertain":
                return CommandResult(-1, "")
            labels = (
                {
                    "heimdall.managed": "true",
                    "heimdall.project-id": str(self.project_id),
                    "heimdall.kind": "gateway",
                }
                if self.gateway_state == "managed"
                else {"heimdall.managed": "false"}
            )
            observation = (
                {"labels": labels, "running": True} if ".State.Running" in command[3] else labels
            )
            return CommandResult(0, json.dumps(observation))
        if command[1:3] == ["network", "inspect"]:
            self.calls.append(command)
            if heartbeat is not None:
                heartbeat()
            if self.network_state == "missing":
                return CommandResult(1, "")
            if self.network_state == "uncertain":
                return CommandResult(-1, "")
            labels = (
                {
                    "heimdall.managed": "true",
                    "heimdall.project-id": str(self.project_id),
                    "heimdall.deployment-id": str(self.deployment_id),
                }
                if self.network_state == "managed"
                else {"heimdall.managed": "false"}
            )
            return CommandResult(0, json.dumps(labels))
        return super().run(
            command,
            timeout_seconds=timeout_seconds,
            heartbeat=heartbeat,
            check=check,
        )


class EdgeConnectorRunner:
    def __init__(self, project_id, network_name: str = "test-heimdall-edge") -> None:
        self.project_id = project_id
        self.network_name = network_name
        self.calls: list[list[str]] = []
        self.network_labels = {
            "heimdall.managed": "true",
            "heimdall.kind": "edge-network",
        }
        self.gateway_labels = {
            "heimdall.managed": "true",
            "heimdall.project-id": str(project_id),
            "heimdall.kind": "gateway",
        }
        self.gateway_running = True
        self.connect_attaches = True
        self.preserve_alias = True
        self.attached = False
        self.aliases: list[str] = []

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
        if command[1:3] == ["network", "inspect"]:
            return CommandResult(0, json.dumps(self.network_labels))
        if command[1] == "inspect" and ".Config.Labels" in command[3]:
            return CommandResult(
                0,
                json.dumps(
                    {
                        "labels": self.gateway_labels,
                        "running": self.gateway_running,
                    }
                ),
            )
        if command[1:3] == ["network", "connect"]:
            if self.connect_attaches:
                self.attached = True
                alias = command[command.index("--alias") + 1]
                self.aliases = [alias] if self.preserve_alias else []
            return CommandResult(0, "")
        if command[1] == "inspect" and ".NetworkSettings.Networks" in command[3]:
            networks = {self.network_name: {"Aliases": self.aliases}} if self.attached else {}
            return CommandResult(0, json.dumps(networks))
        raise AssertionError(f"unexpected Docker command: {command}")


class RecordingEdgeConnector:
    def __init__(self, *, fail_on_attempts: set[int] | None = None) -> None:
        self.project_ids: list = []
        self.fail_on_attempts = fail_on_attempts or set()

    def ensure_gateway_attached(self, project_id, *, heartbeat) -> None:
        self.project_ids.append(project_id)
        heartbeat()
        if len(self.project_ids) in self.fail_on_attempts:
            raise RuntimeFailure(
                "ACTIVATION",
                "GATEWAY_EDGE_NETWORK_CONNECT_FAILED",
                retryable=True,
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

    monkeypatch.setattr(
        "heimdall.runtime.gateway_probe.urlopen", lambda request, timeout: Response()
    )

    observation = HttpRouteProbe().observe(
        "http://127.0.0.1:48080/",
        timeout_seconds=1,
        heartbeat=lambda: None,
    )

    assert observation == GatewayObservation(True, deployment_id)


def test_edge_network_connector_validates_and_confirms_the_deterministic_alias() -> None:
    project_id = uuid4()
    runner = EdgeConnectorRunner(project_id)
    progress = Progress()
    connector = DockerEdgeNetworkConnector(
        runner,
        network_name=runner.network_name,
    )

    connector.ensure_gateway_attached(project_id, heartbeat=progress.heartbeat)

    gateway_name = project_gateway_name(project_id)
    alias = project_gateway_alias(project_id)
    assert runner.calls == [
        [
            "docker",
            "network",
            "inspect",
            "--format",
            "{{json .Labels}}",
            runner.network_name,
        ],
        [
            "docker",
            "inspect",
            "--format",
            '{"labels":{{json .Config.Labels}},"running":{{json .State.Running}}}',
            gateway_name,
        ],
        [
            "docker",
            "network",
            "connect",
            "--alias",
            alias,
            runner.network_name,
            gateway_name,
        ],
        [
            "docker",
            "inspect",
            "--format",
            "{{json .NetworkSettings.Networks}}",
            gateway_name,
        ],
    ]
    assert progress.heartbeats == 4


def test_edge_network_connector_refuses_unmanaged_network_before_mutation() -> None:
    project_id = uuid4()
    runner = EdgeConnectorRunner(project_id)
    runner.network_labels["heimdall.managed"] = "false"
    connector = DockerEdgeNetworkConnector(runner, network_name=runner.network_name)

    with pytest.raises(RuntimeFailure) as raised:
        connector.ensure_gateway_attached(project_id, heartbeat=lambda: None)

    assert raised.value.code == "EDGE_NETWORK_NAME_CONFLICT"
    assert not any(call[1:3] == ["network", "connect"] for call in runner.calls)


def test_edge_network_connector_refuses_wrong_project_gateway_before_mutation() -> None:
    project_id = uuid4()
    runner = EdgeConnectorRunner(project_id)
    runner.gateway_labels["heimdall.project-id"] = str(uuid4())
    connector = DockerEdgeNetworkConnector(runner, network_name=runner.network_name)

    with pytest.raises(RuntimeFailure) as raised:
        connector.ensure_gateway_attached(project_id, heartbeat=lambda: None)

    assert raised.value.code == "GATEWAY_NAME_CONFLICT"
    assert not any(call[1:3] == ["network", "connect"] for call in runner.calls)


def test_edge_network_connector_defers_stopped_gateway_before_mutation() -> None:
    project_id = uuid4()
    runner = EdgeConnectorRunner(project_id)
    runner.gateway_running = False
    connector = DockerEdgeNetworkConnector(runner, network_name=runner.network_name)

    with pytest.raises(RuntimeFailure) as raised:
        connector.ensure_gateway_attached(project_id, heartbeat=lambda: None)

    assert raised.value.code == "GATEWAY_START_FAILED"
    assert raised.value.retryable is True
    assert not any(call[1:3] == ["network", "connect"] for call in runner.calls)


def test_edge_network_connector_requires_the_alias_after_connect() -> None:
    project_id = uuid4()
    runner = EdgeConnectorRunner(project_id)
    runner.preserve_alias = False
    connector = DockerEdgeNetworkConnector(runner, network_name=runner.network_name)

    with pytest.raises(RuntimeFailure) as raised:
        connector.ensure_gateway_attached(project_id, heartbeat=lambda: None)

    assert raised.value.code == "GATEWAY_EDGE_NETWORK_CONNECT_FAILED"


def test_gateway_activates_candidate_and_persists_stable_preview(tmp_path: Path) -> None:
    item = runtime_deployment()
    runtime = RuntimeDeployment.from_deployment(item)
    repository = MemoryRuntimes()
    docker = GatewayDocker()
    runner = GatewayRunner()
    route = HealthyRoute()
    edge = RecordingEdgeConnector()
    activator = NginxGatewayActivator(
        repository,
        docker,
        runner,
        route,
        tmp_path / "gateways",
        edge_network_connector=edge,
    )

    activator.activate(item, runtime, candidate(), Progress())

    assert repository.item is not None
    assert repository.item.active_deployment_id == item.id
    assert repository.item.preview_port == 48080
    assert docker.promoted == candidate()
    assert edge.project_ids == [item.project_id]
    assert route.urls == ["http://127.0.0.1:48080/"]
    config = (tmp_path / "gateways" / item.project_id.hex / "current.conf").read_text()
    assert f"api-g-{item.id.hex[:12]}" in config
    assert f"# deployment: {item.id}" in config
    assert f'add_header X-Heimdall-Deployment-Id "{item.id}" always;' in config


def test_gateway_uses_configured_host_for_preview_probe(tmp_path: Path) -> None:
    item = runtime_deployment()
    runtime = RuntimeDeployment.from_deployment(item)
    repository = MemoryRuntimes()
    docker = GatewayDocker()
    runner = GatewayRunner()
    route = HealthyRoute()
    activator = NginxGatewayActivator(
        repository,
        docker,
        runner,
        route,
        tmp_path / "gateways",
        probe_host="host.docker.internal",
    )

    activator.activate(item, runtime, candidate(), Progress())

    assert route.urls == ["http://host.docker.internal:48080/"]


def test_failed_route_probe_restores_last_known_good_config(tmp_path: Path) -> None:
    item = runtime_deployment()
    runtime = RuntimeDeployment.from_deployment(item)
    repository = MemoryRuntimes()
    docker = GatewayDocker()
    runner = RollbackScopeRunner(item.project_id, item.id)
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


def test_rollback_candidate_requires_exact_gateway_and_network_before_mutation(
    tmp_path: Path,
) -> None:
    item = runtime_deployment()
    repository = MemoryRuntimes()
    runner = RollbackScopeRunner(item.project_id, item.id)
    directory = tmp_path / "gateways" / item.project_id.hex
    directory.mkdir(parents=True)
    current = directory / "current.conf"
    current.write_text(f"# deployment: {item.id}\n# candidate\n")
    last_good = directory / "last-good.config"
    last_good.write_text("# previous\n")
    activator = NginxGatewayActivator(
        repository,
        GatewayDocker(),
        runner,
        HealthyRoute(),
        tmp_path / "gateways",
    )

    activator.rollback_candidate(item)

    gateway_name = project_gateway_name(item.project_id)
    network_name = f"hm-p{item.project_id.hex[:12]}-g{item.id.hex[:12]}"
    assert current.read_text() == "# previous\n"
    assert runner.calls == [
        ["docker", "inspect", "--format", "{{json .Config.Labels}}", gateway_name],
        ["docker", "exec", gateway_name, "nginx", "-s", "reload"],
        ["docker", "network", "inspect", "--format", "{{json .Labels}}", network_name],
        ["docker", "network", "disconnect", "--force", network_name, gateway_name],
    ]


@pytest.mark.parametrize("gateway_state", ["unmanaged", "missing", "uncertain"])
def test_rollback_candidate_does_not_mutate_unverified_gateway(
    tmp_path: Path,
    gateway_state: str,
) -> None:
    item = runtime_deployment()
    runner = RollbackScopeRunner(
        item.project_id,
        item.id,
        gateway_state=gateway_state,
    )
    directory = tmp_path / "gateways" / item.project_id.hex
    directory.mkdir(parents=True)
    current = directory / "current.conf"
    current.write_text(f"# deployment: {item.id}\n# candidate\n")
    (directory / "last-good.config").write_text("# previous\n")
    activator = NginxGatewayActivator(
        MemoryRuntimes(),
        GatewayDocker(),
        runner,
        HealthyRoute(),
        tmp_path / "gateways",
    )

    activator.rollback_candidate(item)

    assert current.read_text() == "# previous\n"
    assert not any(call[1] == "exec" for call in runner.calls)
    assert not any(call[1:3] == ["network", "disconnect"] for call in runner.calls)


@pytest.mark.parametrize("network_state", ["unmanaged", "missing", "uncertain"])
def test_rollback_candidate_does_not_disconnect_unverified_generation_network(
    tmp_path: Path,
    network_state: str,
) -> None:
    item = runtime_deployment()
    runner = RollbackScopeRunner(
        item.project_id,
        item.id,
        network_state=network_state,
    )
    activator = NginxGatewayActivator(
        MemoryRuntimes(),
        GatewayDocker(),
        runner,
        HealthyRoute(),
        tmp_path / "gateways",
    )

    activator.rollback_candidate(item)

    assert any(call[1:3] == ["network", "inspect"] for call in runner.calls)
    assert not any(call[1:3] == ["network", "disconnect"] for call in runner.calls)


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
    edge = RecordingEdgeConnector()
    activator = NginxGatewayActivator(
        repository,
        GatewayDocker(),
        runner,
        route,
        tmp_path / "gateways",
        edge_network_connector=edge,
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
    assert edge.project_ids == [item.project_id, item.project_id]


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


def test_activation_rebases_running_gateway_on_candidate_network_and_stable_port(
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
    runner = ExistingGatewayRunner(item.project_id)
    route = HealthyRoute()
    edge = RecordingEdgeConnector()
    activator = NginxGatewayActivator(
        repository,
        GatewayDocker(),
        runner,
        route,
        tmp_path / "gateways",
        edge_network_connector=edge,
    )

    activator.activate(item, runtime, candidate(), Progress())

    assert ["docker", "rm", "--force", gateway_name] in runner.calls
    create_calls = [call for call in runner.calls if call[1:3] == ["run", "--detach"]]
    assert [call[call.index("--network") + 1] for call in create_calls] == [
        candidate().network_name
    ]
    assert [call[call.index("--publish") + 1] for call in create_calls] == ["127.0.0.1:48080:8080"]
    assert route.urls == ["http://127.0.0.1:48080/", "http://127.0.0.1:48080/"]
    assert edge.project_ids == [item.project_id, item.project_id]


def test_failed_running_gateway_rebase_restores_previous_gateway(tmp_path: Path) -> None:
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
    directory = tmp_path / "gateways" / item.project_id.hex
    directory.mkdir(parents=True)
    previous_config = f"# deployment: {previous_id}\nserver {{ listen 8080; }}\n"
    (directory / "current.conf").write_text(previous_config)
    (directory / "last-good.config").write_text(previous_config)
    runner = StoppedGatewayRunner(item.project_id, fail_detached_run=1)
    runner.gateway_running = True
    docker = GatewayDocker()
    route = HealthyRoute()
    activator = NginxGatewayActivator(
        repository,
        docker,
        runner,
        route,
        tmp_path / "gateways",
    )

    with pytest.raises(RuntimeFailure) as raised:
        activator.activate(item, runtime, candidate(), Progress())

    assert raised.value.code == "GATEWAY_START_FAILED"
    assert repository.item.active_deployment_id == previous_id
    assert repository.item.active_network_name == "previous-network"
    assert runner.gateway_exists is True
    assert runner.gateway_running is True
    create_calls = [call for call in runner.calls if call[1:3] == ["run", "--detach"]]
    assert [call[call.index("--network") + 1] for call in create_calls] == [
        candidate().network_name,
        "previous-network",
    ]
    assert route.urls == ["http://127.0.0.1:48080/"]
    assert (directory / "current.conf").read_text() == previous_config
    assert docker.promoted is None
    assert docker.retired == []


def test_failed_edge_attachment_during_rebase_restores_previous_gateway(
    tmp_path: Path,
) -> None:
    item = runtime_deployment()
    runtime = RuntimeDeployment.from_deployment(item)
    previous_id = uuid4()
    gateway_name = project_gateway_name(item.project_id)
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
    directory = tmp_path / "gateways" / item.project_id.hex
    directory.mkdir(parents=True)
    previous_config = f"# deployment: {previous_id}\nserver {{ listen 8080; }}\n"
    (directory / "current.conf").write_text(previous_config)
    (directory / "last-good.config").write_text(previous_config)
    runner = StoppedGatewayRunner(item.project_id)
    runner.gateway_running = True
    edge = RecordingEdgeConnector(fail_on_attempts={2})
    docker = GatewayDocker()
    route = HealthyRoute()
    activator = NginxGatewayActivator(
        repository,
        docker,
        runner,
        route,
        tmp_path / "gateways",
        edge_network_connector=edge,
    )

    with pytest.raises(RuntimeFailure) as raised:
        activator.activate(item, runtime, candidate(), Progress())

    assert raised.value.code == "GATEWAY_EDGE_NETWORK_CONNECT_FAILED"
    assert repository.item.active_deployment_id == previous_id
    assert repository.item.active_network_name == "previous-network"
    assert runner.gateway_exists is True
    assert runner.gateway_running is True
    create_calls = [call for call in runner.calls if call[1:3] == ["run", "--detach"]]
    assert [call[call.index("--network") + 1] for call in create_calls] == [
        candidate().network_name,
        "previous-network",
    ]
    assert edge.project_ids == [item.project_id, item.project_id, item.project_id]
    assert route.urls == ["http://127.0.0.1:48080/"]
    assert (directory / "current.conf").read_text() == previous_config
    assert docker.promoted is None
    assert docker.retired == []


def test_failed_edge_attachment_during_previous_gateway_restore_is_reported(
    tmp_path: Path,
) -> None:
    item = runtime_deployment()
    runtime = RuntimeDeployment.from_deployment(item)
    previous_id = uuid4()
    gateway_name = project_gateway_name(item.project_id)
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
    directory = tmp_path / "gateways" / item.project_id.hex
    directory.mkdir(parents=True)
    previous_config = f"# deployment: {previous_id}\nserver {{ listen 8080; }}\n"
    (directory / "current.conf").write_text(previous_config)
    (directory / "last-good.config").write_text(previous_config)
    runner = StoppedGatewayRunner(item.project_id)
    runner.gateway_running = True
    edge = RecordingEdgeConnector(fail_on_attempts={2, 3})
    activator = NginxGatewayActivator(
        repository,
        GatewayDocker(),
        runner,
        HealthyRoute(),
        tmp_path / "gateways",
        edge_network_connector=edge,
    )

    with pytest.raises(RuntimeFailure) as raised:
        activator.activate(item, runtime, candidate(), Progress())

    assert raised.value.code == "GATEWAY_EDGE_NETWORK_CONNECT_FAILED"
    assert repository.item.active_deployment_id == previous_id
    assert repository.item.active_network_name == "previous-network"
    assert runner.gateway_exists is True
    assert runner.gateway_running is True
    assert edge.project_ids == [item.project_id, item.project_id, item.project_id]
    assert (directory / "current.conf").read_text() == previous_config


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


def test_recovery_preserves_candidate_when_edge_attachment_cannot_be_confirmed(
    tmp_path: Path,
) -> None:
    item = runtime_deployment()
    runtime = RuntimeDeployment.from_deployment(item)
    previous_id = uuid4()
    repository = MemoryRuntimes()
    repository.item = ProjectRuntime(
        project_id=item.project_id,
        gateway_container_name=project_gateway_name(item.project_id),
        preview_port=48080,
        active_deployment_id=previous_id,
        active_network_name="previous-network",
        active_container_names=("previous-container",),
        active_image_names=("previous-image",),
        updated_at=datetime.now(UTC),
    )
    docker = GatewayDocker(candidate())
    edge = RecordingEdgeConnector(fail_on_attempts={1})
    activator = NginxGatewayActivator(
        repository,
        docker,
        ExistingGatewayRunner(item.project_id),
        HealthyRoute(item.id),
        tmp_path / "gateways",
        edge_network_connector=edge,
    )

    disposition = activator.recover(item, runtime, Progress())

    assert disposition is RecoveryDisposition.UNCERTAIN
    assert repository.item.active_deployment_id == previous_id
    assert edge.project_ids == [item.project_id]
    assert docker.promoted is None


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
