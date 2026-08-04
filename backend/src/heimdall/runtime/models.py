from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from heimdall.deployments.models import Deployment

_SERVICE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_ENVIRONMENT_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_HTTP_PATH = re.compile(r"^/(?:[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*)?$")


class RuntimeConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RuntimeEnvironment:
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class RuntimeSecret:
    name: str
    reference: str
    fingerprint: str
    container_path: str


@dataclass(frozen=True, slots=True)
class RuntimeDatabase:
    host: str
    port: int
    database_name: str
    username: str
    schema_name: str
    credential_reference: str
    credential_fingerprint: str
    container_path: str = "/run/secrets/heimdall/project-database-password"


@dataclass(frozen=True, slots=True)
class RuntimeService:
    name: str
    build_context: PurePosixPath
    dockerfile: PurePosixPath
    internal_port: int
    health_path: str
    environment: tuple[RuntimeEnvironment, ...]
    secrets: tuple[RuntimeSecret, ...]
    project_database_access: bool


@dataclass(frozen=True, slots=True)
class RuntimeRoute:
    path: str
    service: str


@dataclass(frozen=True, slots=True)
class RuntimeDeployment:
    services: tuple[RuntimeService, ...]
    routes: tuple[RuntimeRoute, ...]
    database: RuntimeDatabase | None

    @classmethod
    def from_deployment(cls, deployment: Deployment) -> RuntimeDeployment:
        snapshot = deployment.config_snapshot
        if not isinstance(snapshot, dict):
            raise RuntimeConfigurationError("deployment snapshot must be an object")
        raw_services = snapshot.get("services")
        raw_routes = snapshot.get("routes")
        if not isinstance(raw_services, list) or not raw_services:
            raise RuntimeConfigurationError("deployment snapshot requires services")
        if not isinstance(raw_routes, list) or not raw_routes:
            raise RuntimeConfigurationError("deployment snapshot requires routes")

        services = tuple(_service(item) for item in raw_services)
        service_names = {item.name for item in services}
        if len(service_names) != len(services):
            raise RuntimeConfigurationError("service names must be unique")
        routes = tuple(_route(item, service_names) for item in raw_routes)
        paths = {item.path for item in routes}
        if len(paths) != len(routes) or "/" not in paths:
            raise RuntimeConfigurationError("routes must be unique and include root")

        database = _database(snapshot.get("managedDatabase"))
        if any(service.project_database_access for service in services) and database is None:
            raise RuntimeConfigurationError("database metadata is required by a service")
        return cls(services=services, routes=routes, database=database)


def _service(value: Any) -> RuntimeService:
    if not isinstance(value, dict):
        raise RuntimeConfigurationError("service must be an object")
    name = value.get("name")
    build = value.get("build")
    port = value.get("internalPort")
    health_path = value.get("healthPath")
    if not isinstance(name, str) or _SERVICE_NAME.fullmatch(name) is None:
        raise RuntimeConfigurationError("invalid service name")
    if not isinstance(build, dict):
        raise RuntimeConfigurationError("service build must be an object")
    context = _relative_path(build.get("context"), "build context")
    dockerfile = _relative_path(build.get("dockerfile"), "Dockerfile")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise RuntimeConfigurationError("invalid internal port")
    if not isinstance(health_path, str) or _HTTP_PATH.fullmatch(health_path) is None:
        raise RuntimeConfigurationError("invalid health path")

    environment: list[RuntimeEnvironment] = []
    secrets: list[RuntimeSecret] = []
    names: set[str] = set()
    raw_environment = value.get("environment", [])
    if not isinstance(raw_environment, list):
        raise RuntimeConfigurationError("service environment must be a list")
    for variable in raw_environment:
        if not isinstance(variable, dict):
            raise RuntimeConfigurationError("environment variable must be an object")
        variable_name = variable.get("name")
        if (
            not isinstance(variable_name, str)
            or _ENVIRONMENT_NAME.fullmatch(variable_name) is None
            or variable_name in names
        ):
            raise RuntimeConfigurationError("invalid or duplicate environment name")
        names.add(variable_name)
        if variable.get("kind") == "PLAIN":
            raw_value = variable.get("value")
            if not isinstance(raw_value, str):
                raise RuntimeConfigurationError("plain environment value must be text")
            environment.append(RuntimeEnvironment(variable_name, raw_value))
        elif variable.get("kind") == "SECRET":
            reference = variable.get("secretReference")
            fingerprint = variable.get("secretFingerprint")
            if not isinstance(reference, str) or not reference:
                raise RuntimeConfigurationError("secret reference is required")
            if not isinstance(fingerprint, str) or _FINGERPRINT.fullmatch(fingerprint) is None:
                raise RuntimeConfigurationError("secret fingerprint is required")
            target = f"/run/secrets/heimdall/environment/{variable_name.lower()}"
            secrets.append(RuntimeSecret(variable_name, reference, fingerprint, target))
        else:
            raise RuntimeConfigurationError("unknown environment kind")

    database_access = value.get("projectDatabaseAccess", False)
    if not isinstance(database_access, bool):
        raise RuntimeConfigurationError("project database access must be boolean")
    return RuntimeService(
        name=name,
        build_context=context,
        dockerfile=dockerfile,
        internal_port=port,
        health_path=health_path,
        environment=tuple(environment),
        secrets=tuple(secrets),
        project_database_access=database_access,
    )


def _route(value: Any, service_names: set[str]) -> RuntimeRoute:
    if not isinstance(value, dict):
        raise RuntimeConfigurationError("route must be an object")
    path = value.get("path")
    service = value.get("service")
    if (
        not isinstance(path, str)
        or _HTTP_PATH.fullmatch(path) is None
        or (path != "/" and path.endswith("/"))
    ):
        raise RuntimeConfigurationError("invalid route path")
    if service not in service_names:
        raise RuntimeConfigurationError("route references an unknown service")
    return RuntimeRoute(path=path, service=service)


def _database(value: Any) -> RuntimeDatabase | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RuntimeConfigurationError("managed database metadata must be an object")
    required_text = (
        "host",
        "databaseName",
        "username",
        "schemaName",
        "credentialReference",
        "credentialFingerprint",
    )
    if any(not isinstance(value.get(key), str) or not value[key] for key in required_text):
        raise RuntimeConfigurationError("managed database metadata is incomplete")
    port = value.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise RuntimeConfigurationError("managed database port is invalid")
    fingerprint = value["credentialFingerprint"]
    if _FINGERPRINT.fullmatch(fingerprint) is None:
        raise RuntimeConfigurationError("managed database fingerprint is invalid")
    return RuntimeDatabase(
        host=value["host"],
        port=port,
        database_name=value["databaseName"],
        username=value["username"],
        schema_name=value["schemaName"],
        credential_reference=value["credentialReference"],
        credential_fingerprint=fingerprint,
    )


def _relative_path(value: Any, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise RuntimeConfigurationError(f"{label} is required")
    if value == ".":
        return PurePosixPath(value)
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeConfigurationError(f"{label} must be a canonical relative path")
    return path
