from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

PROBE_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    runtime_root: Path
    broker_socket_root: Path
    runtime_probe_host: str
    git_executable: str
    git_workspace_root: Path
    git_timeout_seconds: float
    recent_commit_limit: int
    project_database_enabled: bool
    project_database_admin_url: str | None
    project_database_runtime_host: str
    project_database_runtime_port: int
    docker_executable: str
    managed_database_container: str
    nginx_image: str
    runtime_command_timeout_seconds: float
    runtime_health_timeout_seconds: float
    service_log_command_timeout_seconds: float
    service_log_broker_timeout_seconds: float
    worker_lease_seconds: float
    worker_poll_seconds: float
    worker_max_attempts: int
    runtime_retention_hours: float

    @classmethod
    def from_environment(cls) -> Settings:
        workspace = Path(
            os.environ.get("HEIMDALL_GIT_WORKSPACE_ROOT", "/tmp/heimdall-python-git")
        ).resolve()
        runtime_root = Path(
            os.environ.get("HEIMDALL_RUNTIME_ROOT", "/tmp/heimdall-python-runtime")
        ).resolve()
        broker_socket_root = Path(
            os.environ.get("HEIMDALL_BROKER_SOCKET_ROOT", str(runtime_root))
        ).resolve()
        runtime_probe_host = os.environ.get("HEIMDALL_RUNTIME_PROBE_HOST", "127.0.0.1")
        if not PROBE_HOST.fullmatch(runtime_probe_host):
            raise ValueError("HEIMDALL_RUNTIME_PROBE_HOST must be a hostname or IPv4 address")
        return cls(
            database_url=os.environ.get(
                "HEIMDALL_DATABASE_URL",
                "postgresql://heimdall:change-me@127.0.0.1:55432/heimdall",
            ),
            runtime_root=runtime_root,
            broker_socket_root=broker_socket_root,
            runtime_probe_host=runtime_probe_host,
            git_executable=os.environ.get("HEIMDALL_GIT_EXECUTABLE", "git"),
            git_workspace_root=workspace,
            git_timeout_seconds=float(os.environ.get("HEIMDALL_GIT_TIMEOUT_SECONDS", "20")),
            recent_commit_limit=int(os.environ.get("HEIMDALL_RECENT_COMMIT_LIMIT", "20")),
            project_database_enabled=os.environ.get("HEIMDALL_PROJECT_DB_ENABLED", "false").lower()
            in {"1", "true", "yes"},
            project_database_admin_url=os.environ.get("HEIMDALL_PROJECT_DB_ADMIN_URL") or None,
            project_database_runtime_host=os.environ.get(
                "HEIMDALL_PROJECT_DB_RUNTIME_HOST", "managed-postgres"
            ),
            project_database_runtime_port=int(
                os.environ.get("HEIMDALL_PROJECT_DB_RUNTIME_PORT", "5432")
            ),
            docker_executable=os.environ.get("HEIMDALL_DOCKER_EXECUTABLE", "docker"),
            managed_database_container=os.environ.get(
                "HEIMDALL_MANAGED_DB_CONTAINER", "heimdall-managed-postgres"
            ),
            nginx_image=os.environ.get("HEIMDALL_NGINX_IMAGE", "nginx:1.29-alpine"),
            runtime_command_timeout_seconds=float(
                os.environ.get("HEIMDALL_RUNTIME_COMMAND_TIMEOUT_SECONDS", "900")
            ),
            runtime_health_timeout_seconds=float(
                os.environ.get("HEIMDALL_RUNTIME_HEALTH_TIMEOUT_SECONDS", "60")
            ),
            service_log_command_timeout_seconds=float(
                os.environ.get("HEIMDALL_SERVICE_LOG_COMMAND_TIMEOUT_SECONDS", "5")
            ),
            service_log_broker_timeout_seconds=float(
                os.environ.get("HEIMDALL_SERVICE_LOG_BROKER_TIMEOUT_SECONDS", "6")
            ),
            worker_lease_seconds=float(os.environ.get("HEIMDALL_WORKER_LEASE_SECONDS", "120")),
            worker_poll_seconds=float(os.environ.get("HEIMDALL_WORKER_POLL_SECONDS", "1")),
            worker_max_attempts=int(os.environ.get("HEIMDALL_WORKER_MAX_ATTEMPTS", "3")),
            runtime_retention_hours=float(os.environ.get("HEIMDALL_RUNTIME_RETENTION_HOURS", "72")),
        )
