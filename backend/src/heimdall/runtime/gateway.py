from __future__ import annotations

import json
import os
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from heimdall.deployments.models import Deployment
from heimdall.deployments.worker import RecoveryDisposition, RuntimeFailure, RuntimeProgress
from heimdall.runtime.docker import CandidateGeneration, DockerRuntime
from heimdall.runtime.gateway_config import default_nginx_config, render_nginx_config
from heimdall.runtime.gateway_probe import GatewayObservation, RouteProbe
from heimdall.runtime.models import RuntimeDeployment
from heimdall.runtime.process import CommandExecutionError, CommandResult, CommandRunner
from heimdall.runtime.repository import ProjectRuntime, RuntimeRepository


@dataclass(frozen=True, slots=True)
class _GatewayReadiness:
    preview_port: int
    needs_network_rebase: bool


class NginxGatewayActivator:
    def __init__(
        self,
        repository: RuntimeRepository,
        docker: DockerRuntime,
        runner: CommandRunner,
        probe: RouteProbe,
        config_root: Path,
        *,
        docker_executable: str = "docker",
        image: str = "nginx:1.29-alpine",
        command_timeout_seconds: float = 120,
        route_timeout_seconds: float = 10,
    ) -> None:
        self._repository = repository
        self._docker = docker
        self._runner = runner
        self._probe = probe
        self._config_root = config_root.resolve()
        self._docker_executable = docker_executable
        self._image = image
        self._command_timeout_seconds = command_timeout_seconds
        self._route_timeout_seconds = route_timeout_seconds

    def is_active(self, deployment: Deployment) -> bool:
        current = self._repository.get(deployment.project_id)
        return current is not None and current.active_deployment_id == deployment.id

    def recover(
        self,
        deployment: Deployment,
        runtime: RuntimeDeployment,
        progress: RuntimeProgress,
    ) -> RecoveryDisposition:
        progress.heartbeat()
        gateway_name = _gateway_name(deployment.project_id.hex)
        stored = self._repository.get(deployment.project_id)
        if stored is not None and stored.active_deployment_id == deployment.id:
            return RecoveryDisposition.ACTIVE

        inspected = self._run_ignored(
            ["inspect", "--format", "{{json .Config.Labels}}", gateway_name],
            heartbeat=progress.heartbeat,
        )
        if inspected.returncode == -1:
            return RecoveryDisposition.UNCERTAIN
        if inspected.returncode != 0:
            if not self._restore_target_file(deployment, gateway_name, progress, reload=False):
                return RecoveryDisposition.UNCERTAIN
            return RecoveryDisposition.SAFE_TO_RETRY
        if not _is_managed_gateway(inspected.stdout, deployment):
            return RecoveryDisposition.UNCERTAIN

        port_result = self._run_ignored(
            ["port", gateway_name, "8080/tcp"], heartbeat=progress.heartbeat
        )
        if port_result.returncode != 0:
            return RecoveryDisposition.UNCERTAIN
        try:
            observed_port = _published_port(port_result.stdout)
            stored = self._repository.ensure_gateway(
                deployment.project_id, gateway_name, observed_port
            )
        except (RuntimeError, RuntimeFailure):
            return RecoveryDisposition.UNCERTAIN
        observation = self._probe.observe(
            f"http://127.0.0.1:{stored.preview_port}/",
            timeout_seconds=self._route_timeout_seconds,
            heartbeat=progress.heartbeat,
        )
        if not observation.reachable:
            return RecoveryDisposition.UNCERTAIN
        if observation.deployment_id == deployment.id:
            candidate = self._docker.observe_candidate(deployment, runtime, progress)
            if candidate is None:
                return (
                    RecoveryDisposition.SAFE_TO_RETRY
                    if self._restore_previous_generation(deployment, stored, gateway_name, progress)
                    else RecoveryDisposition.UNCERTAIN
                )
            try:
                self._docker.verify_candidate(runtime, candidate, progress)
                for route in runtime.routes:
                    self._probe.probe(
                        f"http://127.0.0.1:{stored.preview_port}{route.path}",
                        timeout_seconds=self._route_timeout_seconds,
                        heartbeat=progress.heartbeat,
                    )
            except RuntimeFailure:
                return (
                    RecoveryDisposition.SAFE_TO_RETRY
                    if self._restore_previous_generation(deployment, stored, gateway_name, progress)
                    else RecoveryDisposition.UNCERTAIN
                )
            previous = self._repository.activate(
                deployment.project_id,
                deployment.id,
                candidate.network_name,
                tuple(item.container_name for item in candidate.services),
                tuple(item.image_name for item in candidate.services),
            )
            directory = self._gateway_directory(deployment.project_id.hex)
            _ensure_private_directory(directory)
            active_config = render_nginx_config(deployment, runtime)
            _atomic_write(directory / "current.conf", active_config)
            _atomic_write(directory / "last-good.config", active_config)
            self._docker.promote_candidate(candidate)
            self._retire_previous(previous, candidate.network_name, gateway_name)
            return RecoveryDisposition.ACTIVE

        if not _matches_previous_generation(
            observation,
            stored,
            self._gateway_directory(deployment.project_id.hex),
        ):
            return RecoveryDisposition.UNCERTAIN
        return (
            RecoveryDisposition.SAFE_TO_RETRY
            if self._restore_previous_generation(deployment, stored, gateway_name, progress)
            else RecoveryDisposition.UNCERTAIN
        )

    def activate(
        self,
        deployment: Deployment,
        runtime: RuntimeDeployment,
        candidate: CandidateGeneration,
        progress: RuntimeProgress,
    ) -> None:
        gateway_name = _gateway_name(deployment.project_id.hex)
        directory = self._gateway_directory(deployment.project_id.hex)
        current_path = directory / "current.conf"
        last_good_path = directory / "last-good.config"
        candidate_path = directory / f"candidate-{deployment.id.hex}.config"
        _ensure_private_directory(directory)
        if not current_path.exists():
            _atomic_write(current_path, default_nginx_config())
            _atomic_write(last_good_path, default_nginx_config())

        readiness = self._ensure_gateway(
            deployment,
            candidate.network_name,
            gateway_name,
            directory,
            progress,
        )
        stored = self._repository.ensure_gateway(
            deployment.project_id,
            gateway_name,
            readiness.preview_port,
        )
        if stored.active_deployment_id == deployment.id:
            return
        self._connect_gateway(candidate.network_name, gateway_name, progress)
        _atomic_write(candidate_path, render_nginx_config(deployment, runtime))
        self._test_config(candidate.network_name, candidate_path, progress)

        previous_config = current_path.read_text(encoding="utf-8")
        switched = False
        rebase_started = False
        try:
            os.replace(candidate_path, current_path)
            switched = True
            self._reload(gateway_name, progress)
            self._probe_routes(runtime, stored.preview_port, progress)
            if readiness.needs_network_rebase:
                rebase_started = True
                self._recreate_gateway_on_network(
                    deployment,
                    candidate.network_name,
                    gateway_name,
                    directory,
                    stored,
                    progress,
                )
                self._probe_routes(runtime, stored.preview_port, progress)
            previous = self._repository.activate(
                deployment.project_id,
                deployment.id,
                candidate.network_name,
                tuple(item.container_name for item in candidate.services),
                tuple(item.image_name for item in candidate.services),
            )
            _atomic_write(last_good_path, current_path.read_text(encoding="utf-8"))
            self._docker.promote_candidate(candidate)
            self._retire_previous(previous, candidate.network_name, gateway_name)
        except Exception as error:
            if switched:
                _atomic_write(current_path, previous_config)
                if rebase_started:
                    with suppress(RuntimeFailure):
                        self._restore_previous_gateway(
                            deployment,
                            gateway_name,
                            directory,
                            stored,
                            progress,
                        )
                else:
                    with suppress(RuntimeFailure):
                        self._reload(gateway_name, progress)
            if isinstance(error, RuntimeFailure):
                raise
            raise RuntimeFailure("ACTIVATION", "GATEWAY_ACTIVATION_FAILED") from error
        finally:
            candidate_path.unlink(missing_ok=True)

    def rollback_candidate(self, deployment: Deployment) -> None:
        current = self._repository.get(deployment.project_id)
        if current is not None and current.active_deployment_id == deployment.id:
            return
        directory = self._gateway_directory(deployment.project_id.hex)
        current_path = directory / "current.conf"
        last_good_path = directory / "last-good.config"
        gateway_name = _gateway_name(deployment.project_id.hex)
        if current_path.exists() and last_good_path.exists():
            marker = f"# deployment: {deployment.id}"
            if marker in current_path.read_text(encoding="utf-8"):
                _atomic_write(current_path, last_good_path.read_text(encoding="utf-8"))
                self._run_ignored(["exec", gateway_name, "nginx", "-s", "reload"])
        self._run_ignored(
            [
                "network",
                "disconnect",
                "--force",
                _candidate_network_name(deployment),
                gateway_name,
            ]
        )

    def _ensure_gateway(
        self,
        deployment: Deployment,
        network_name: str,
        gateway_name: str,
        directory: Path,
        progress: RuntimeProgress,
    ) -> _GatewayReadiness:
        inspected = self._run_ignored(
            [
                "inspect",
                "--format",
                '{"labels":{{json .Config.Labels}},"running":{{json .State.Running}}}',
                gateway_name,
            ]
        )
        stored = self._repository.get(deployment.project_id)
        needs_network_rebase = False
        if inspected.returncode != 0:
            self._start_gateway(
                deployment,
                network_name,
                gateway_name,
                directory,
                stored,
                progress,
            )
        else:
            running = _managed_gateway_running(inspected.stdout, deployment)
            if running:
                needs_network_rebase = (
                    stored is not None
                    and stored.active_network_name is not None
                    and stored.active_network_name != network_name
                )
            else:
                if stored is not None and stored.gateway_container_name != gateway_name:
                    raise RuntimeFailure("ACTIVATION", "GATEWAY_NAME_CONFLICT")
                self._run(
                    ["rm", gateway_name],
                    progress,
                    RuntimeFailure("ACTIVATION", "GATEWAY_START_FAILED", retryable=True),
                )
                restore_network = (
                    stored.active_network_name
                    if stored is not None and stored.active_network_name is not None
                    else network_name
                )
                needs_network_rebase = restore_network != network_name
                self._start_gateway(
                    deployment,
                    restore_network,
                    gateway_name,
                    directory,
                    stored,
                    progress,
                )
        result = self._run(
            ["port", gateway_name, "8080/tcp"],
            progress,
            RuntimeFailure("ACTIVATION", "GATEWAY_PORT_UNAVAILABLE"),
        )
        return _GatewayReadiness(
            preview_port=_published_port(result.stdout),
            needs_network_rebase=needs_network_rebase,
        )

    def _start_gateway(
        self,
        deployment: Deployment,
        network_name: str,
        gateway_name: str,
        directory: Path,
        stored: ProjectRuntime | None,
        progress: RuntimeProgress,
    ) -> None:
        published = (
            f"127.0.0.1:{stored.preview_port}:8080" if stored is not None else "127.0.0.1::8080"
        )
        self._run(
            [
                "run",
                "--detach",
                "--name",
                gateway_name,
                "--network",
                network_name,
                "--publish",
                published,
                "--restart",
                "unless-stopped",
                "--label",
                "heimdall.managed=true",
                "--label",
                f"heimdall.project-id={deployment.project_id}",
                "--label",
                "heimdall.kind=gateway",
                "--mount",
                f"type=bind,src={directory},dst=/etc/nginx/conf.d,readonly",
                self._image,
            ],
            progress,
            RuntimeFailure("ACTIVATION", "GATEWAY_START_FAILED", retryable=True),
        )

    def _recreate_gateway_on_network(
        self,
        deployment: Deployment,
        network_name: str,
        gateway_name: str,
        directory: Path,
        stored: ProjectRuntime,
        progress: RuntimeProgress,
    ) -> None:
        self._remove_managed_gateway(
            deployment,
            gateway_name,
            progress,
            force=True,
            missing_ok=False,
        )
        self._start_gateway(
            deployment,
            network_name,
            gateway_name,
            directory,
            stored,
            progress,
        )
        result = self._run(
            ["port", gateway_name, "8080/tcp"],
            progress,
            RuntimeFailure("ACTIVATION", "GATEWAY_PORT_UNAVAILABLE"),
        )
        if _published_port(result.stdout) != stored.preview_port:
            raise RuntimeFailure("ACTIVATION", "GATEWAY_PORT_UNAVAILABLE")

    def _restore_previous_gateway(
        self,
        deployment: Deployment,
        gateway_name: str,
        directory: Path,
        stored: ProjectRuntime,
        progress: RuntimeProgress,
    ) -> None:
        if stored.active_network_name is None:
            raise RuntimeFailure("ACTIVATION", "GATEWAY_START_FAILED", retryable=True)
        self._remove_managed_gateway(
            deployment,
            gateway_name,
            progress,
            force=True,
            missing_ok=True,
        )
        self._start_gateway(
            deployment,
            stored.active_network_name,
            gateway_name,
            directory,
            stored,
            progress,
        )

    def _remove_managed_gateway(
        self,
        deployment: Deployment,
        gateway_name: str,
        progress: RuntimeProgress,
        *,
        force: bool,
        missing_ok: bool,
    ) -> None:
        inspected = self._run_ignored(
            [
                "inspect",
                "--format",
                '{"labels":{{json .Config.Labels}},"running":{{json .State.Running}}}',
                gateway_name,
            ],
            heartbeat=progress.heartbeat,
        )
        if inspected.returncode != 0:
            if missing_ok and inspected.returncode != -1:
                return
            raise RuntimeFailure("ACTIVATION", "GATEWAY_START_FAILED", retryable=True)
        _managed_gateway_running(inspected.stdout, deployment)
        arguments = ["rm"]
        if force:
            arguments.append("--force")
        arguments.append(gateway_name)
        self._run(
            arguments,
            progress,
            RuntimeFailure("ACTIVATION", "GATEWAY_START_FAILED", retryable=True),
        )

    def _probe_routes(
        self,
        runtime: RuntimeDeployment,
        preview_port: int,
        progress: RuntimeProgress,
    ) -> None:
        for route in runtime.routes:
            self._probe.probe(
                f"http://127.0.0.1:{preview_port}{route.path}",
                timeout_seconds=self._route_timeout_seconds,
                heartbeat=progress.heartbeat,
            )

    def _connect_gateway(
        self, network_name: str, gateway_name: str, progress: RuntimeProgress
    ) -> None:
        result = self._run_ignored(["network", "connect", network_name, gateway_name])
        if result.returncode not in {0, 1}:
            raise RuntimeFailure("ACTIVATION", "GATEWAY_NETWORK_CONNECT_FAILED")
        progress.heartbeat()

    def _test_config(
        self, network_name: str, candidate_path: Path, progress: RuntimeProgress
    ) -> None:
        self._run(
            [
                "run",
                "--rm",
                "--network",
                network_name,
                "--mount",
                (f"type=bind,src={candidate_path},dst=/etc/nginx/conf.d/default.conf,readonly"),
                self._image,
                "nginx",
                "-t",
            ],
            progress,
            RuntimeFailure("ACTIVATION", "NGINX_CONFIG_INVALID"),
        )

    def _reload(self, gateway_name: str, progress: RuntimeProgress) -> None:
        self._run(
            ["exec", gateway_name, "nginx", "-s", "reload"],
            progress,
            RuntimeFailure("ACTIVATION", "GATEWAY_RELOAD_FAILED", retryable=True),
        )

    def _retire_previous(
        self,
        previous: ProjectRuntime | None,
        active_network_name: str,
        gateway_name: str,
    ) -> None:
        if (
            previous is None
            or previous.active_deployment_id is None
            or previous.active_network_name is None
            or previous.active_network_name == active_network_name
        ):
            return
        self._run_ignored(["network", "disconnect", previous.active_network_name, gateway_name])
        self._docker.retire_generation(
            previous.active_network_name,
            previous.active_container_names,
            previous.active_image_names,
            previous.active_deployment_id,
        )

    def _gateway_directory(self, project_hex: str) -> Path:
        return self._config_root / project_hex

    def _restore_target_file(
        self,
        deployment: Deployment,
        gateway_name: str,
        progress: RuntimeProgress,
        *,
        reload: bool,
    ) -> bool:
        directory = self._gateway_directory(deployment.project_id.hex)
        current_path = directory / "current.conf"
        last_good_path = directory / "last-good.config"
        if not current_path.exists():
            return True
        marker = f"# deployment: {deployment.id}"
        if marker not in current_path.read_text(encoding="utf-8"):
            return True
        if not last_good_path.exists():
            return False
        _atomic_write(current_path, last_good_path.read_text(encoding="utf-8"))
        if reload:
            try:
                self._reload(gateway_name, progress)
            except RuntimeFailure:
                return False
        return True

    def _restore_previous_generation(
        self,
        deployment: Deployment,
        stored: ProjectRuntime,
        gateway_name: str,
        progress: RuntimeProgress,
    ) -> bool:
        if not self._restore_target_file(deployment, gateway_name, progress, reload=True):
            return False
        observation = self._probe.observe(
            f"http://127.0.0.1:{stored.preview_port}/",
            timeout_seconds=self._route_timeout_seconds,
            heartbeat=progress.heartbeat,
        )
        if not observation.reachable or not _matches_previous_generation(
            observation,
            stored,
            self._gateway_directory(deployment.project_id.hex),
        ):
            return False
        self._run_ignored(
            [
                "network",
                "disconnect",
                "--force",
                _candidate_network_name(deployment),
                gateway_name,
            ],
            heartbeat=progress.heartbeat,
        )
        return True

    def _run(
        self,
        arguments: list[str],
        progress: RuntimeProgress,
        failure: RuntimeFailure | None = None,
    ):
        try:
            return self._runner.run(
                [self._docker_executable, *arguments],
                timeout_seconds=self._command_timeout_seconds,
                heartbeat=progress.heartbeat,
            )
        except CommandExecutionError as error:
            resolved = failure or RuntimeFailure(
                "ACTIVATION", "GATEWAY_COMMAND_FAILED", retryable=True
            )
            raise resolved from error

    def _run_ignored(self, arguments: list[str], *, heartbeat: Callable[[], None] | None = None):
        try:
            return self._runner.run(
                [self._docker_executable, *arguments],
                timeout_seconds=self._command_timeout_seconds,
                heartbeat=heartbeat,
                check=False,
            )
        except CommandExecutionError:
            return CommandResult(-1, "")


def _gateway_name(project_hex: str) -> str:
    return f"hm-p{project_hex[:12]}-gateway"


def _candidate_network_name(deployment: Deployment) -> str:
    return f"hm-p{deployment.project_id.hex[:12]}-g{deployment.id.hex[:12]}"


def _published_port(output: str) -> int:
    for value in output.splitlines():
        _, separator, raw_port = value.strip().rpartition(":")
        if separator and raw_port.isdigit() and 1 <= int(raw_port) <= 65535:
            return int(raw_port)
    raise RuntimeFailure("ACTIVATION", "GATEWAY_PORT_UNAVAILABLE")


def _is_managed_gateway(output: str, deployment: Deployment) -> bool:
    try:
        labels = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return False
    return _has_managed_gateway_labels(labels, deployment)


def _managed_gateway_running(output: str, deployment: Deployment) -> bool:
    try:
        observation = json.loads(output)
    except (json.JSONDecodeError, TypeError) as error:
        raise RuntimeFailure("ACTIVATION", "GATEWAY_NAME_CONFLICT") from error
    if not isinstance(observation, dict):
        raise RuntimeFailure("ACTIVATION", "GATEWAY_NAME_CONFLICT")
    labels = observation.get("labels")
    running = observation.get("running")
    if not _has_managed_gateway_labels(labels, deployment) or not isinstance(running, bool):
        raise RuntimeFailure("ACTIVATION", "GATEWAY_NAME_CONFLICT")
    return running


def _has_managed_gateway_labels(labels: object, deployment: Deployment) -> bool:
    return (
        isinstance(labels, dict)
        and labels.get("heimdall.managed") == "true"
        and labels.get("heimdall.project-id") == str(deployment.project_id)
        and labels.get("heimdall.kind") == "gateway"
    )


def _matches_previous_generation(
    observation: GatewayObservation, stored: ProjectRuntime, directory: Path
) -> bool:
    if observation.deployment_id == stored.active_deployment_id:
        return True
    if observation.deployment_id is not None:
        return False
    if stored.active_deployment_id is None:
        return True
    last_good_path = directory / "last-good.config"
    return last_good_path.exists() and (
        f"# deployment: {stored.active_deployment_id}" in last_good_path.read_text(encoding="utf-8")
    )


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeFailure("ACTIVATION", "GATEWAY_CONFIG_ROOT_INVALID")
    os.chmod(path, 0o700)


def _atomic_write(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)
