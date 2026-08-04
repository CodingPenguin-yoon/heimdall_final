from __future__ import annotations

import os
import signal
import socket
from datetime import timedelta
from threading import Event

from heimdall.config import Settings
from heimdall.database import Database
from heimdall.deployments.repository import PostgresDeploymentRepository
from heimdall.deployments.worker import DeploymentWorker
from heimdall.git.client import GitClient
from heimdall.projects.repository import PostgresProjectRepository
from heimdall.projects.service import ProjectService
from heimdall.runtime.docker import DockerRuntime, HttpHealthProbe
from heimdall.runtime.gateway import HttpRouteProbe, NginxGatewayActivator
from heimdall.runtime.process import SubprocessCommandRunner
from heimdall.runtime.repository import PostgresRuntimeRepository
from heimdall.runtime.service import DockerDeploymentProcessor
from heimdall.secrets.store import FileSecretStore


def run(settings: Settings | None = None, stop: Event | None = None) -> None:
    app_settings = settings or Settings.from_environment()
    stop_event = stop or Event()
    database = Database(app_settings.database_url)
    database.open()
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
        runtimes = PostgresRuntimeRepository(database)
        docker = DockerRuntime(
            runner,
            HttpHealthProbe(),
            executable=app_settings.docker_executable,
            managed_database_container=app_settings.managed_database_container,
            command_timeout_seconds=app_settings.runtime_command_timeout_seconds,
            health_timeout_seconds=app_settings.runtime_health_timeout_seconds,
        )
        gateway = NginxGatewayActivator(
            runtimes,
            docker,
            runner,
            HttpRouteProbe(),
            app_settings.runtime_root / "gateways",
            docker_executable=app_settings.docker_executable,
            image=app_settings.nginx_image,
            command_timeout_seconds=app_settings.runtime_command_timeout_seconds,
        )
        processor = DockerDeploymentProcessor(
            projects,
            git,
            docker,
            gateway,
            secret_store,
            app_settings.git_workspace_root,
        )
        worker = DeploymentWorker(
            deployments,
            processor,
            worker_id=f"{socket.gethostname()}:{os.getpid()}",
            lease_duration=timedelta(seconds=app_settings.worker_lease_seconds),
            max_attempts=app_settings.worker_max_attempts,
        )
        while not stop_event.is_set():
            if not worker.run_once():
                stop_event.wait(app_settings.worker_poll_seconds)
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
