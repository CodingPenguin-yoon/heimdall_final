from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

PROBE_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
DOCKER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


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
    nginx_image: str
    runtime_command_timeout_seconds: float
    runtime_health_timeout_seconds: float
    service_log_command_timeout_seconds: float
    service_log_broker_timeout_seconds: float
    worker_lease_seconds: float
    worker_poll_seconds: float
    worker_max_attempts: int
    runtime_retention_hours: float
    diagnostic_retention_days: float = 30
    auth_secret_root: Path = Path("/run/secrets/heimdall/auth")
    auth_secret_source_root: Path = Path("/run/secrets/heimdall/auth")
    management_hostname: str = "heimdall.localhost"
    deployment_base_domain: str = "deployments.localhost"
    reserved_public_subdomains: tuple[str, ...] = ("admin", "api", "www")
    edge_config_root: Path = Path("/tmp/heimdall-python-edge")
    edge_network_name: str = "heimdall-edge"
    edge_container_name: str = "heimdall-edge-gateway"
    edge_nginx_image: str = "nginx:1.29-alpine"
    edge_probe_host: str = "127.0.0.1"
    edge_http_port: int = 8088
    routing_worker_lease_seconds: float = 60
    routing_worker_poll_seconds: float = 1
    routing_worker_max_attempts: int = 3
    routing_worker_retry_seconds: float = 5
    routing_worker_retry_max_seconds: float = 60

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
        management_hostname = _hostname(
            "HEIMDALL_MANAGEMENT_HOSTNAME",
            os.environ.get("HEIMDALL_MANAGEMENT_HOSTNAME", "heimdall.localhost"),
        )
        deployment_base_domain = _hostname(
            "HEIMDALL_DEPLOYMENT_BASE_DOMAIN",
            os.environ.get("HEIMDALL_DEPLOYMENT_BASE_DOMAIN", "deployments.localhost"),
        )
        if management_hostname == deployment_base_domain or management_hostname.endswith(
            f".{deployment_base_domain}"
        ):
            raise ValueError(
                "HEIMDALL_MANAGEMENT_HOSTNAME must be outside the deployment base domain"
            )
        configured_reserved = os.environ.get("HEIMDALL_RESERVED_PUBLIC_SUBDOMAINS", "")
        reserved = {"admin", "api", "www", management_hostname.split(".", 1)[0]}
        for raw_label in configured_reserved.split(","):
            if not raw_label.strip():
                continue
            label = raw_label.strip().lower()
            if DNS_LABEL.fullmatch(label) is None or "--" in label:
                raise ValueError(
                    "HEIMDALL_RESERVED_PUBLIC_SUBDOMAINS must contain lowercase DNS labels"
                )
            reserved.add(label)
        edge_probe_host = os.environ.get("HEIMDALL_EDGE_PROBE_HOST", "127.0.0.1")
        if not PROBE_HOST.fullmatch(edge_probe_host):
            raise ValueError("HEIMDALL_EDGE_PROBE_HOST must be a hostname or IPv4 address")
        edge_http_port = int(os.environ.get("HEIMDALL_EDGE_HTTP_PORT", "8088"))
        if not 1 <= edge_http_port <= 65535:
            raise ValueError("HEIMDALL_EDGE_HTTP_PORT must be between 1 and 65535")
        auth_secret_root = Path(
            os.environ.get("HEIMDALL_AUTH_SECRET_ROOT", "/run/secrets/heimdall/auth")
        )
        if not auth_secret_root.is_absolute():
            raise ValueError("HEIMDALL_AUTH_SECRET_ROOT must be an absolute path")
        auth_secret_source_root = _absolute_lexical_path(
            "HEIMDALL_AUTH_SECRET_SOURCE_ROOT",
            os.environ.get("HEIMDALL_AUTH_SECRET_SOURCE_ROOT", str(auth_secret_root)),
        )
        edge_config_root = Path(
            os.environ.get("HEIMDALL_EDGE_CONFIG_ROOT", "/tmp/heimdall-python-edge")
        ).resolve()
        for shared_root_name, shared_root in (
            ("HEIMDALL_RUNTIME_ROOT", runtime_root),
            ("HEIMDALL_GIT_WORKSPACE_ROOT", workspace),
            ("HEIMDALL_EDGE_CONFIG_ROOT", edge_config_root),
        ):
            if _paths_overlap(auth_secret_source_root, shared_root):
                raise ValueError(
                    f"HEIMDALL_AUTH_SECRET_SOURCE_ROOT must not overlap {shared_root_name}"
                )
        settings = cls(
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
                "HEIMDALL_PROJECT_DB_RUNTIME_HOST", "host.docker.internal"
            ),
            project_database_runtime_port=int(
                os.environ.get("HEIMDALL_PROJECT_DB_RUNTIME_PORT", "55433")
            ),
            docker_executable=os.environ.get("HEIMDALL_DOCKER_EXECUTABLE", "docker"),
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
            diagnostic_retention_days=float(
                os.environ.get("HEIMDALL_DIAGNOSTIC_RETENTION_DAYS", "30")
            ),
            auth_secret_root=auth_secret_root,
            auth_secret_source_root=auth_secret_source_root,
            management_hostname=management_hostname,
            deployment_base_domain=deployment_base_domain,
            reserved_public_subdomains=tuple(sorted(reserved)),
            edge_config_root=edge_config_root,
            edge_network_name=os.environ.get("HEIMDALL_EDGE_NETWORK_NAME", "heimdall-edge"),
            edge_container_name=os.environ.get(
                "HEIMDALL_EDGE_CONTAINER_NAME", "heimdall-edge-gateway"
            ),
            edge_nginx_image=os.environ.get("HEIMDALL_EDGE_NGINX_IMAGE", "nginx:1.29-alpine"),
            edge_probe_host=edge_probe_host,
            edge_http_port=edge_http_port,
            routing_worker_lease_seconds=float(
                os.environ.get("HEIMDALL_ROUTING_WORKER_LEASE_SECONDS", "60")
            ),
            routing_worker_poll_seconds=float(
                os.environ.get("HEIMDALL_ROUTING_WORKER_POLL_SECONDS", "1")
            ),
            routing_worker_max_attempts=int(
                os.environ.get("HEIMDALL_ROUTING_WORKER_MAX_ATTEMPTS", "3")
            ),
            routing_worker_retry_seconds=float(
                os.environ.get("HEIMDALL_ROUTING_WORKER_RETRY_SECONDS", "5")
            ),
            routing_worker_retry_max_seconds=float(
                os.environ.get("HEIMDALL_ROUTING_WORKER_RETRY_MAX_SECONDS", "60")
            ),
        )
        if not DOCKER_NAME.fullmatch(settings.edge_network_name):
            raise ValueError("HEIMDALL_EDGE_NETWORK_NAME must be a Docker resource name")
        if not DOCKER_NAME.fullmatch(settings.edge_container_name):
            raise ValueError("HEIMDALL_EDGE_CONTAINER_NAME must be a Docker resource name")
        if not settings.edge_nginx_image.strip():
            raise ValueError("HEIMDALL_EDGE_NGINX_IMAGE must not be blank")
        if settings.routing_worker_lease_seconds <= 0:
            raise ValueError("HEIMDALL_ROUTING_WORKER_LEASE_SECONDS must be positive")
        if settings.routing_worker_poll_seconds <= 0:
            raise ValueError("HEIMDALL_ROUTING_WORKER_POLL_SECONDS must be positive")
        if settings.routing_worker_max_attempts < 1:
            raise ValueError("HEIMDALL_ROUTING_WORKER_MAX_ATTEMPTS must be positive")
        if settings.routing_worker_retry_seconds <= 0:
            raise ValueError("HEIMDALL_ROUTING_WORKER_RETRY_SECONDS must be positive")
        if settings.routing_worker_retry_max_seconds < settings.routing_worker_retry_seconds:
            raise ValueError(
                "HEIMDALL_ROUTING_WORKER_RETRY_MAX_SECONDS must be at least the retry delay"
            )
        return settings


def _hostname(name: str, value: str) -> str:
    hostname = value.strip().lower().rstrip(".")
    if len(hostname) > 253 or "." not in hostname:
        raise ValueError(f"{name} must be a canonical hostname")
    if any(DNS_LABEL.fullmatch(label) is None for label in hostname.split(".")):
        raise ValueError(f"{name} must be a canonical hostname")
    return hostname


def _absolute_lexical_path(name: str, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return Path(os.path.normpath(path))


def _paths_overlap(left: Path, right: Path) -> bool:
    normalized_left = Path(os.path.normpath(left))
    normalized_right = Path(os.path.normpath(right))
    return (
        normalized_left == normalized_right
        or normalized_left.is_relative_to(normalized_right)
        or normalized_right.is_relative_to(normalized_left)
    )
