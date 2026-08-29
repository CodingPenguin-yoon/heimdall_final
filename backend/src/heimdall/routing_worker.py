from __future__ import annotations

import logging
import os
import signal
import socket
from datetime import timedelta
from threading import Event

from heimdall.config import Settings
from heimdall.database import Database
from heimdall.public_routes.repository import PostgresPublicRouteRepository
from heimdall.public_routes.worker import PublicRouteWorker
from heimdall.runtime.edge import DockerEdgeConfigManager, EdgeRouteProbe
from heimdall.runtime.edge_network import DockerEdgeNetworkConnector
from heimdall.runtime.process import SubprocessCommandRunner

logger = logging.getLogger(__name__)


def run(settings: Settings | None = None, stop: Event | None = None) -> None:
    app_settings = settings or Settings.from_environment()
    stop_event = stop or Event()
    runner = SubprocessCommandRunner(
        heartbeat_interval_seconds=max(1, app_settings.routing_worker_lease_seconds / 3)
    )
    edge_config = DockerEdgeConfigManager(
        runner,
        EdgeRouteProbe(
            app_settings.edge_probe_host,
            app_settings.edge_http_port,
            timeout_seconds=app_settings.runtime_health_timeout_seconds,
        ),
        app_settings.edge_config_root,
        app_settings.management_hostname,
        docker_executable=app_settings.docker_executable,
        nginx_image=app_settings.edge_nginx_image,
        edge_network_name=app_settings.edge_network_name,
        edge_container_name=app_settings.edge_container_name,
        command_timeout_seconds=app_settings.runtime_command_timeout_seconds,
    )
    if edge_config.recover_interrupted():
        logger.warning("validated a pending Edge routing transaction for DB reconciliation")
    database = Database(app_settings.database_url)
    database.open()
    try:
        repository = PostgresPublicRouteRepository(database)
        edge_network = DockerEdgeNetworkConnector(
            runner,
            executable=app_settings.docker_executable,
            network_name=app_settings.edge_network_name,
            command_timeout_seconds=app_settings.runtime_command_timeout_seconds,
        )
        worker = PublicRouteWorker(
            repository,
            edge_network,
            edge_config,
            worker_id=f"{socket.gethostname()}:{os.getpid()}:routing",
            lease_duration=timedelta(seconds=app_settings.routing_worker_lease_seconds),
            max_attempts=app_settings.routing_worker_max_attempts,
            retry_base_delay=timedelta(seconds=app_settings.routing_worker_retry_seconds),
            retry_max_delay=timedelta(seconds=app_settings.routing_worker_retry_max_seconds),
        )
        startup_reconciled = False
        while not stop_event.is_set():
            if not startup_reconciled:
                startup_reconciled = worker.reconcile_startup()
                if not startup_reconciled:
                    stop_event.wait(app_settings.routing_worker_poll_seconds)
                    continue
            if worker.run_once():
                continue
            stop_event.wait(app_settings.routing_worker_poll_seconds)
    finally:
        database.close()


def main() -> None:
    stop = Event()

    def request_stop(signum, frame) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    run(stop=stop)


if __name__ == "__main__":
    main()
