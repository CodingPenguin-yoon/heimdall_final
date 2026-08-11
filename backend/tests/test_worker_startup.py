from datetime import UTC, datetime

from test_runtime_models import runtime_deployment

from heimdall.runtime.repository import ProjectRuntime
from heimdall.worker import restore_active_database_networks


class ActiveRuntimes:
    def __init__(self, item: ProjectRuntime) -> None:
        self.item = item

    def list_active(self) -> list[ProjectRuntime]:
        return [self.item]


class Deployments:
    def __init__(self, item) -> None:
        self.item = item

    def get(self, deployment_id):
        assert deployment_id == self.item.id
        return self.item


class DatabaseNetworks:
    def __init__(self) -> None:
        self.restored: list[tuple] = []

    def restore_active_database_network(self, deployment, runtime, network_name) -> bool:
        self.restored.append((deployment, runtime, network_name))
        return True


def test_worker_startup_restores_active_database_networks() -> None:
    deployment = runtime_deployment()
    network_name = f"hm-p{deployment.project_id.hex[:12]}-g{deployment.id.hex[:12]}"
    runtime = ProjectRuntime(
        project_id=deployment.project_id,
        gateway_container_name=f"hm-p{deployment.project_id.hex[:12]}-gateway",
        preview_port=48080,
        active_deployment_id=deployment.id,
        active_network_name=network_name,
        active_container_names=(),
        active_image_names=(),
        updated_at=datetime.now(UTC),
    )
    docker = DatabaseNetworks()

    restore_active_database_networks(Deployments(deployment), ActiveRuntimes(runtime), docker)

    assert len(docker.restored) == 1
    assert docker.restored[0][0] == deployment
    assert docker.restored[0][2] == network_name
