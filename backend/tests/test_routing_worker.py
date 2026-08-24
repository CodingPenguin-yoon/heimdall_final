from __future__ import annotations

from threading import Event
from types import SimpleNamespace

from heimdall import routing_worker


def test_interrupted_validation_precedes_database_and_failed_reconciliation_skips_jobs(
    monkeypatch,
    tmp_path,
) -> None:
    events: list[str] = []
    stop = Event()

    class Database:
        def __init__(self, url) -> None:
            events.append("database-init")

        def open(self) -> None:
            events.append("database-open")

        def close(self) -> None:
            events.append("database-close")

    class EdgeConfig:
        def __init__(self, *args, **kwargs) -> None:
            events.append("edge-config-init")

        def recover_interrupted(self) -> bool:
            events.append("recover-interrupted")
            return True

    class Worker:
        def __init__(self, *args, **kwargs) -> None:
            return None

        def reconcile_startup(self) -> bool:
            events.append("reconcile-startup")
            stop.set()
            return False

        def run_once(self) -> bool:
            events.append("run-once")
            return False

    monkeypatch.setattr(routing_worker, "Database", Database)
    monkeypatch.setattr(routing_worker, "DockerEdgeConfigManager", EdgeConfig)
    monkeypatch.setattr(routing_worker, "PublicRouteWorker", Worker)
    monkeypatch.setattr(
        routing_worker,
        "PostgresPublicRouteRepository",
        lambda database: object(),
    )
    monkeypatch.setattr(
        routing_worker,
        "DockerEdgeNetworkConnector",
        lambda *args, **kwargs: object(),
    )
    settings = SimpleNamespace(
        database_url="postgresql://unused",
        docker_executable="docker",
        edge_config_root=tmp_path / "edge",
        edge_container_name="heimdall-edge-gateway",
        edge_http_port=8088,
        edge_network_name="heimdall-edge",
        edge_nginx_image="nginx:1.29-alpine",
        edge_probe_host="127.0.0.1",
        management_hostname="control.management.test",
        routing_worker_lease_seconds=60,
        routing_worker_max_attempts=3,
        routing_worker_poll_seconds=0.001,
        routing_worker_retry_max_seconds=60,
        routing_worker_retry_seconds=5,
        runtime_command_timeout_seconds=120,
        runtime_health_timeout_seconds=10,
    )

    routing_worker.run(settings, stop)

    assert events.index("edge-config-init") < events.index("database-open")
    assert events.index("recover-interrupted") < events.index("database-open")
    assert "run-once" not in events
    assert events[-1] == "database-close"
