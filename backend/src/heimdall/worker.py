from __future__ import annotations

import logging
import os
import signal
import socket
from datetime import timedelta
from threading import Event

from heimdall.config import Settings
from heimdall.database import Database
from heimdall.deployments.models import DeploymentNotFoundError
from heimdall.deployments.repository import PostgresDeploymentRepository
from heimdall.deployments.service import DeploymentService
from heimdall.deployments.worker import DeploymentWorker
from heimdall.git.client import GitClient
from heimdall.projects.repository import PostgresProjectRepository
from heimdall.projects.service import ProjectService
from heimdall.public_routes.repository import PostgresPublicRouteRepository
from heimdall.public_routes.service import PublicRouteService
from heimdall.runtime.deployment_diagnostics import DockerDeploymentDiagnosticCollector
from heimdall.runtime.docker import DockerRuntime, HttpHealthProbe
from heimdall.runtime.docker_logs import DockerServiceLogReader, DockerServiceLogStreamer
from heimdall.runtime.edge_network import DockerEdgeNetworkConnector
from heimdall.runtime.gateway import NginxGatewayActivator
from heimdall.runtime.gateway_probe import HttpRouteProbe
from heimdall.runtime.log_broker import UnixServiceLogBrokerServer, service_log_socket_path
from heimdall.runtime.log_stream_broker import (
    UnixServiceLogStreamBrokerServer,
    service_log_stream_socket_path,
)
from heimdall.runtime.logs import ServiceLogError
from heimdall.runtime.process import SubprocessCommandRunner
from heimdall.runtime.process_stream import SubprocessCommandStreamRunner
from heimdall.runtime.reconciliation_repository import PostgresRuntimeReconciliationRepository
from heimdall.runtime.reconciliation_worker import RuntimeReconciliationWorker
from heimdall.runtime.repository import PostgresRuntimeRepository
from heimdall.runtime.service import DockerDeploymentProcessor
from heimdall.secrets.store import FileSecretStore

logger = logging.getLogger(__name__)


def run(settings: Settings | None = None, stop: Event | None = None) -> None:
    app_settings = settings or Settings.from_environment()
    stop_event = stop or Event()
    database = Database(app_settings.database_url)
    database.open()
    log_broker: UnixServiceLogBrokerServer | None = None
    log_stream_broker: UnixServiceLogStreamBrokerServer | None = None
    try:
        runner = SubprocessCommandRunner(
            heartbeat_interval_seconds=max(1, app_settings.worker_lease_seconds / 3)
        )
        secret_store = FileSecretStore(app_settings.runtime_root)
        git = GitClient(
            executable=app_settings.git_executable,
            timeout_seconds=app_settings.git_timeout_seconds,
            recent_commit_limit=app_settings.recent_commit_limit,
        )
        projects = ProjectService(PostgresProjectRepository(database), git, secret_store)
        deployments = PostgresDeploymentRepository(database)
        deployment_service = DeploymentService(deployments, projects)
        public_routes = PublicRouteService(
            PostgresPublicRouteRepository(database),
            projects,
            app_settings.deployment_base_domain,
            app_settings.reserved_public_subdomains,
        )
        runtimes = PostgresRuntimeRepository(database)
        docker = DockerRuntime(
            runner,
            HttpHealthProbe(),
            executable=app_settings.docker_executable,
            command_timeout_seconds=app_settings.runtime_command_timeout_seconds,
            health_timeout_seconds=app_settings.runtime_health_timeout_seconds,
            probe_host=app_settings.runtime_probe_host,
        )
        log_reader = DockerServiceLogReader(
            runner,
            secret_store,
            executable=app_settings.docker_executable,
            command_timeout_seconds=app_settings.service_log_command_timeout_seconds,
        )
        log_streamer = DockerServiceLogStreamer(
            runner,
            SubprocessCommandStreamRunner(),
            secret_store,
            executable=app_settings.docker_executable,
            command_timeout_seconds=app_settings.service_log_command_timeout_seconds,
        )

        def read_service_logs(deployment_id, service_name):
            try:
                deployment = deployments.get(deployment_id)
            except DeploymentNotFoundError as error:
                raise ServiceLogError("SERVICE_LOGS_UNAVAILABLE") from error
            return log_reader.read(deployment, service_name)

        def stream_service_logs(deployment_id, service_name):
            try:
                deployment = deployments.get(deployment_id)
            except DeploymentNotFoundError as error:
                raise ServiceLogError("SERVICE_LOGS_UNAVAILABLE") from error
            return log_streamer.open(deployment, service_name)

        candidate_broker = UnixServiceLogBrokerServer(
            service_log_socket_path(app_settings.broker_socket_root),
            read_service_logs,
        )
        try:
            candidate_broker.start()
        except OSError:
            logger.warning(
                "service log broker could not start; deployment processing continues",
                exc_info=True,
            )
        else:
            log_broker = candidate_broker
        candidate_stream_broker = UnixServiceLogStreamBrokerServer(
            service_log_stream_socket_path(app_settings.broker_socket_root),
            stream_service_logs,
        )
        try:
            candidate_stream_broker.start()
        except OSError:
            logger.warning(
                "service log stream broker could not start; deployment processing continues",
                exc_info=True,
            )
        else:
            log_stream_broker = candidate_stream_broker
        gateway = NginxGatewayActivator(
            runtimes,
            docker,
            runner,
            HttpRouteProbe(),
            app_settings.runtime_root / "gateways",
            docker_executable=app_settings.docker_executable,
            image=app_settings.nginx_image,
            command_timeout_seconds=app_settings.runtime_command_timeout_seconds,
            probe_host=app_settings.runtime_probe_host,
            edge_network_connector=DockerEdgeNetworkConnector(
                runner,
                executable=app_settings.docker_executable,
                network_name=app_settings.edge_network_name,
                command_timeout_seconds=app_settings.runtime_command_timeout_seconds,
            ),
        )
        processor = DockerDeploymentProcessor(
            projects,
            git,
            docker,
            gateway,
            secret_store,
            app_settings.git_workspace_root,
            DockerDeploymentDiagnosticCollector(log_reader, secret_store),
        )
        worker_id = f"{socket.gethostname()}:{os.getpid()}"
        lease_duration = timedelta(seconds=app_settings.worker_lease_seconds)
        worker = DeploymentWorker(
            deployments,
            processor,
            worker_id=worker_id,
            lease_duration=lease_duration,
            max_attempts=app_settings.worker_max_attempts,
            diagnostic_retention=timedelta(days=app_settings.diagnostic_retention_days),
            on_runtime_ready=public_routes.wake_pending_for_runtime,
        )
        reconciliation_worker = RuntimeReconciliationWorker(
            PostgresRuntimeReconciliationRepository(database),
            deployment_service,
            processor,
            worker_id=worker_id,
            lease_duration=lease_duration,
            retention_duration=timedelta(hours=app_settings.runtime_retention_hours),
            max_attempts=app_settings.worker_max_attempts,
            diagnostic_retention=timedelta(days=app_settings.diagnostic_retention_days),
            on_runtime_ready=public_routes.wake_pending_for_runtime,
        )
        while not stop_event.is_set():
            if worker.run_once():
                continue
            if not reconciliation_worker.run_once():
                stop_event.wait(app_settings.worker_poll_seconds)
    finally:
        if log_stream_broker is not None:
            log_stream_broker.stop()
        if log_broker is not None:
            log_broker.stop()
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
