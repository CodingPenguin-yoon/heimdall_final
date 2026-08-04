from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.client import RemoteDisconnected
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID

from heimdall.deployments.models import Deployment, DeploymentStatus
from heimdall.deployments.worker import RuntimeFailure, RuntimeProgress
from heimdall.runtime.models import RuntimeDatabase, RuntimeDeployment, RuntimeService
from heimdall.runtime.process import CommandExecutionError, CommandResult, CommandRunner
from heimdall.secrets.store import SecretStore, SecretStoreError


class HealthProbe(Protocol):
    def wait_until_healthy(
        self,
        url: str,
        *,
        timeout_seconds: float,
        heartbeat: Callable[[], None],
    ) -> None: ...


class HttpHealthProbe:
    def __init__(self, interval_seconds: float = 0.5) -> None:
        self._interval_seconds = interval_seconds

    def wait_until_healthy(
        self,
        url: str,
        *,
        timeout_seconds: float,
        heartbeat: Callable[[], None],
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                request = Request(url, method="GET")
                with urlopen(request, timeout=min(2, timeout_seconds)) as response:
                    if 200 <= response.status < 400:
                        return
            except (HTTPError, URLError, TimeoutError, RemoteDisconnected, ConnectionError):
                pass
            heartbeat()
            time.sleep(self._interval_seconds)
        raise RuntimeFailure("HEALTH_CHECK", "SERVICE_HEALTH_TIMEOUT")


@dataclass(frozen=True, slots=True)
class RunningService:
    name: str
    container_name: str
    image_name: str
    health_port: int


@dataclass(frozen=True, slots=True)
class CandidateGeneration:
    network_name: str
    services: tuple[RunningService, ...]


class DockerRuntime:
    def __init__(
        self,
        runner: CommandRunner,
        probe: HealthProbe,
        *,
        executable: str = "docker",
        managed_database_container: str = "heimdall-managed-postgres",
        command_timeout_seconds: float = 900,
        health_timeout_seconds: float = 60,
    ) -> None:
        self._runner = runner
        self._probe = probe
        self._executable = executable
        self._managed_database_container = managed_database_container
        self._command_timeout_seconds = command_timeout_seconds
        self._health_timeout_seconds = health_timeout_seconds

    def start_candidate(
        self,
        deployment: Deployment,
        runtime: RuntimeDeployment,
        source_root: Path,
        secret_store: SecretStore,
        progress: RuntimeProgress,
    ) -> CandidateGeneration:
        source_root = source_root.resolve(strict=True)
        self.cleanup_candidate(deployment, runtime)
        for service in runtime.services:
            self._assert_absent("container", _container_name(deployment, service))
            self._assert_absent("image", _image_name(deployment, service))
        self._assert_absent("network", _network_name(deployment))
        progress.stage(
            DeploymentStatus.BUILDING,
            "IMAGES_BUILDING",
            "Building service images from the selected commit",
        )
        for service in runtime.services:
            context, dockerfile = _build_paths(source_root, service)
            self._run(
                [
                    "build",
                    "--label",
                    "heimdall.managed=true",
                    "--label",
                    f"heimdall.project-id={deployment.project_id}",
                    "--label",
                    f"heimdall.deployment-id={deployment.id}",
                    "--file",
                    str(dockerfile),
                    "--tag",
                    _image_name(deployment, service),
                    str(context),
                ],
                progress,
                RuntimeFailure("BUILD", "IMAGE_BUILD_FAILED"),
            )

        network = _network_name(deployment)
        self._run(
            [
                "network",
                "create",
                "--label",
                "heimdall.managed=true",
                "--label",
                f"heimdall.project-id={deployment.project_id}",
                "--label",
                f"heimdall.deployment-id={deployment.id}",
                network,
            ],
            progress,
            RuntimeFailure("DOCKER", "NETWORK_CREATE_FAILED", retryable=True),
        )
        if any(service.project_database_access for service in runtime.services):
            database = runtime.database
            if database is None:
                raise RuntimeFailure("CONFIGURATION", "DATABASE_METADATA_MISSING")
            self._run(
                [
                    "network",
                    "connect",
                    "--alias",
                    database.host,
                    network,
                    self._managed_database_container,
                ],
                progress,
                RuntimeFailure("DOCKER", "DATABASE_NETWORK_CONNECT_FAILED", retryable=True),
            )

        progress.stage(
            DeploymentStatus.STARTING,
            "SERVICES_STARTING",
            "Starting generation service containers",
        )
        running: list[RunningService] = []
        for service in runtime.services:
            arguments = self._run_arguments(
                deployment,
                runtime.database,
                service,
                network,
                secret_store,
            )
            self._run(
                arguments,
                progress,
                RuntimeFailure("START", "SERVICE_START_FAILED", retryable=True),
            )
            container = _container_name(deployment, service)
            port_result = self._run(
                ["port", container, f"{service.internal_port}/tcp"],
                progress,
                RuntimeFailure("START", "HEALTH_PORT_UNAVAILABLE"),
            )
            health_port = _published_port(port_result.stdout)
            running.append(
                RunningService(
                    name=service.name,
                    container_name=container,
                    image_name=_image_name(deployment, service),
                    health_port=health_port,
                )
            )

        progress.stage(
            DeploymentStatus.HEALTH_CHECKING,
            "SERVICES_HEALTH_CHECKING",
            "Waiting for all candidate services to become healthy",
        )
        for service, item in zip(runtime.services, running, strict=True):
            self._probe.wait_until_healthy(
                f"http://127.0.0.1:{item.health_port}{service.health_path}",
                timeout_seconds=self._health_timeout_seconds,
                heartbeat=progress.heartbeat,
            )
        return CandidateGeneration(network_name=network, services=tuple(running))

    def cleanup_candidate(self, deployment: Deployment, runtime: RuntimeDeployment) -> None:
        for service in runtime.services:
            name = _container_name(deployment, service)
            if self._is_managed("container", name, deployment.id):
                self._run_ignored(["rm", "--force", name])
        network = _network_name(deployment)
        if self._is_managed("network", network, deployment.id):
            self._disconnect_managed_database(network)
            self._run_ignored(["network", "rm", network])
        for service in runtime.services:
            image = _image_name(deployment, service)
            if self._is_managed("image", image, deployment.id):
                self._run_ignored(["image", "rm", "--force", image])

    def promote_candidate(self, candidate: CandidateGeneration) -> None:
        for service in candidate.services:
            self._run_ignored(["update", "--restart", "unless-stopped", service.container_name])

    def retire_generation(
        self,
        network_name: str,
        container_names: tuple[str, ...],
        image_names: tuple[str, ...],
        deployment_id: UUID,
    ) -> None:
        for container_name in container_names:
            if self._is_managed("container", container_name, deployment_id):
                self._run_ignored(["rm", "--force", container_name])
        if self._is_managed("network", network_name, deployment_id):
            self._disconnect_managed_database(network_name)
            self._run_ignored(["network", "rm", network_name])
        for image_name in image_names:
            if self._is_managed("image", image_name, deployment_id):
                self._run_ignored(["image", "rm", "--force", image_name])

    def _is_managed(self, kind: str, name: str, deployment_id: UUID) -> bool:
        result = self._inspect(kind, name)
        if result.returncode != 0:
            return False
        try:
            labels = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            return False
        return (
            isinstance(labels, dict)
            and labels.get("heimdall.managed") == "true"
            and labels.get("heimdall.deployment-id") == str(deployment_id)
        )

    def _assert_absent(self, kind: str, name: str) -> None:
        if self._inspect(kind, name).returncode == 0:
            raise RuntimeFailure("DOCKER", "RESOURCE_NAME_CONFLICT")

    def _disconnect_managed_database(self, network_name: str) -> None:
        self._run_ignored(
            [
                "network",
                "disconnect",
                "--force",
                network_name,
                self._managed_database_container,
            ]
        )

    def _inspect(self, kind: str, name: str):
        if kind == "network":
            arguments = ["network", "inspect", "--format", "{{json .Labels}}", name]
        elif kind == "image":
            arguments = ["image", "inspect", "--format", "{{json .Config.Labels}}", name]
        else:
            arguments = ["inspect", "--format", "{{json .Config.Labels}}", name]
        return self._run_ignored(arguments)

    def _run_arguments(
        self,
        deployment: Deployment,
        database: RuntimeDatabase | None,
        service: RuntimeService,
        network: str,
        secret_store: SecretStore,
    ) -> list[str]:
        arguments = [
            "run",
            "--detach",
            "--name",
            _container_name(deployment, service),
            "--network",
            network,
            "--network-alias",
            service.name,
            "--network-alias",
            _gateway_alias(deployment, service),
            "--publish",
            f"127.0.0.1::{service.internal_port}",
            "--label",
            "heimdall.managed=true",
            "--label",
            f"heimdall.project-id={deployment.project_id}",
            "--label",
            f"heimdall.deployment-id={deployment.id}",
            "--env",
            f"HEIMDALL_PROJECT_ID={deployment.project_id}",
            "--env",
            f"HEIMDALL_DEPLOYMENT_ID={deployment.id}",
        ]
        for variable in service.environment:
            arguments.extend(["--env", f"{variable.name}={variable.value}"])
        try:
            for secret in service.secrets:
                source = secret_store.resolve(secret.reference, secret.fingerprint)
                arguments.extend(["--env", f"{secret.name}={secret.container_path}"])
                arguments.extend(["--mount", _bind_mount(source, secret.container_path)])
            if service.project_database_access:
                if database is None:
                    raise RuntimeFailure("CONFIGURATION", "DATABASE_METADATA_MISSING")
                credential = secret_store.resolve(
                    database.credential_reference,
                    database.credential_fingerprint,
                )
                managed_values = {
                    "DATABASE_HOST": database.host,
                    "DATABASE_PORT": str(database.port),
                    "DATABASE_NAME": database.database_name,
                    "DATABASE_USER": database.username,
                    "DATABASE_SCHEMA": database.schema_name,
                    "DATABASE_PASSWORD_FILE": database.container_path,
                }
                for name, value in managed_values.items():
                    arguments.extend(["--env", f"{name}={value}"])
                arguments.extend(["--mount", _bind_mount(credential, database.container_path)])
        except SecretStoreError as error:
            raise RuntimeFailure("SECRET", "SECRET_RESOLUTION_FAILED") from error
        arguments.append(_image_name(deployment, service))
        return arguments

    def _run(
        self,
        arguments: list[str],
        progress: RuntimeProgress,
        failure: RuntimeFailure | None = None,
    ):
        try:
            return self._runner.run(
                [self._executable, *arguments],
                timeout_seconds=self._command_timeout_seconds,
                heartbeat=progress.heartbeat,
            )
        except CommandExecutionError as error:
            resolved = failure or RuntimeFailure("DOCKER", "DOCKER_COMMAND_FAILED", retryable=True)
            raise resolved from error

    def _run_ignored(self, arguments: list[str]):
        try:
            return self._runner.run(
                [self._executable, *arguments],
                timeout_seconds=self._command_timeout_seconds,
                check=False,
            )
        except CommandExecutionError:
            return CommandResult(-1, "")


def _build_paths(source_root: Path, service: RuntimeService) -> tuple[Path, Path]:
    context = source_root.joinpath(*service.build_context.parts).resolve()
    dockerfile = context.joinpath(*service.dockerfile.parts).resolve()
    if not context.is_relative_to(source_root) or not context.is_dir():
        raise RuntimeFailure("SOURCE", "BUILD_CONTEXT_INVALID")
    if not dockerfile.is_relative_to(context) or not dockerfile.is_file():
        raise RuntimeFailure("SOURCE", "DOCKERFILE_INVALID")
    return context, dockerfile


def _network_name(deployment: Deployment) -> str:
    return f"hm-p{deployment.project_id.hex[:12]}-g{deployment.id.hex[:12]}"


def _container_name(deployment: Deployment, service: RuntimeService) -> str:
    return f"hm-p{deployment.project_id.hex[:12]}-{service.name}-g{deployment.id.hex[:12]}"


def _image_name(deployment: Deployment, service: RuntimeService) -> str:
    return f"heimdall/{deployment.project_id.hex}:g{deployment.id.hex[:12]}-{service.name}"


def _gateway_alias(deployment: Deployment, service: RuntimeService) -> str:
    return f"{service.name}-g-{deployment.id.hex[:12]}"


def _bind_mount(source: Path, destination: str) -> str:
    if not source.is_absolute() or not source.is_file():
        raise RuntimeFailure("SECRET", "SECRET_FILE_INVALID")
    if "," in str(source):
        raise RuntimeFailure("SECRET", "SECRET_PATH_INVALID")
    return f"type=bind,src={source},dst={destination},readonly"


def _published_port(output: str) -> int:
    candidates = [line.strip() for line in output.splitlines() if line.strip()]
    for value in candidates:
        _, separator, raw_port = value.rpartition(":")
        if separator and raw_port.isdigit() and 1 <= int(raw_port) <= 65535:
            return int(raw_port)
    raise RuntimeFailure("START", "HEALTH_PORT_UNAVAILABLE")
