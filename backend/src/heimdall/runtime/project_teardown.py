from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from heimdall.deployments.models import Deployment
from heimdall.deployments.worker import RuntimeFailure
from heimdall.runtime.gateway_identity import project_gateway_name
from heimdall.runtime.models import RuntimeConfigurationError, RuntimeDeployment
from heimdall.runtime.process import CommandExecutionError, CommandResult, CommandRunner
from heimdall.runtime.repository import ProjectRuntime


@dataclass(frozen=True, slots=True)
class _ExpectedResource:
    docker_kind: str
    label_kind: str
    name: str
    deployment_id: UUID | None


class ProjectRuntimeTeardown:
    """Remove one project's runtime after the caller proves its deletion fence.

    ``mutation_guard`` must atomically re-check both the live deletion intent and that
    the public route is absent. It is called immediately before every Docker or
    filesystem mutation; returning false prevents that mutation.
    """

    def __init__(
        self,
        runner: CommandRunner,
        workspace_root: Path,
        gateway_config_root: Path,
        *,
        docker_executable: str = "docker",
        command_timeout_seconds: float = 120,
    ) -> None:
        self._runner = runner
        self._workspace_root = workspace_root.absolute()
        self._gateway_config_root = gateway_config_root.absolute()
        self._docker_executable = docker_executable
        self._command_timeout_seconds = command_timeout_seconds

    def teardown(
        self,
        project_id: UUID,
        deployments: Sequence[Deployment],
        runtime: ProjectRuntime | None,
        *,
        mutation_guard: Callable[[], bool],
        heartbeat: Callable[[], bool | None],
    ) -> None:
        expected = self._expected_resources(project_id, deployments, runtime)
        filesystem_targets = (
            *((self._workspace_root, deployment.id.hex) for deployment in deployments),
            (self._gateway_config_root, project_id.hex),
        )

        self._pulse(heartbeat)
        states = self._observe_expected(project_id, expected, heartbeat)
        self._reject_extra_resources(project_id, expected, heartbeat)
        for root, basename in filesystem_targets:
            _validate_exact_private_child(root, basename)

        removal_order = {"gateway": 0, "service": 1, "network": 2, "image": 3}
        for resource in sorted(expected, key=lambda item: removal_order[item.label_kind]):
            observed_id = states[resource]
            if observed_id is None:
                continue
            self._authorize(mutation_guard, heartbeat)
            current_id = self._observe_one(project_id, resource, heartbeat)
            if current_id != observed_id:
                self._uncertain()
            result = self._mutate_remove(resource, observed_id, heartbeat)
            if result.returncode != 0:
                self._uncertain()

        remaining = self._observe_expected(project_id, expected, heartbeat)
        if any(item is not None for item in remaining.values()):
            self._uncertain()
        self._reject_extra_resources(project_id, expected, heartbeat, require_empty=True)

        for root, basename in filesystem_targets:
            _remove_exact_private_child(
                root,
                basename,
                authorize=lambda: self._authorize(mutation_guard, heartbeat),
            )
            if _exact_child_exists(root, basename):
                self._filesystem_uncertain()

        final_states = self._observe_expected(project_id, expected, heartbeat)
        if any(item is not None for item in final_states.values()):
            self._uncertain()
        self._reject_extra_resources(project_id, expected, heartbeat, require_empty=True)

    def verify_absent(
        self,
        project_id: UUID,
        deployments: Sequence[Deployment],
        runtime: ProjectRuntime | None,
        *,
        mutation_guard: Callable[[], bool],
        heartbeat: Callable[[], bool | None],
    ) -> None:
        self._authorize(mutation_guard, heartbeat)
        expected = self._expected_resources(project_id, deployments, runtime)
        states = self._observe_expected(project_id, expected, heartbeat)
        if any(item is not None for item in states.values()):
            self._uncertain()
        self._reject_extra_resources(project_id, expected, heartbeat, require_empty=True)
        filesystem_targets = (
            *((self._workspace_root, deployment.id.hex) for deployment in deployments),
            (self._gateway_config_root, project_id.hex),
        )
        if any(_exact_child_exists(root, basename) for root, basename in filesystem_targets):
            self._filesystem_uncertain()

    def _expected_resources(
        self,
        project_id: UUID,
        deployments: Sequence[Deployment],
        runtime: ProjectRuntime | None,
    ) -> tuple[_ExpectedResource, ...]:
        resources: list[_ExpectedResource] = []
        if runtime is not None:
            if (
                runtime.project_id != project_id
                or runtime.gateway_container_name != project_gateway_name(project_id)
            ):
                self._uncertain()
            resources.append(
                _ExpectedResource(
                    docker_kind="container",
                    label_kind="gateway",
                    name=project_gateway_name(project_id),
                    deployment_id=None,
                )
            )

        deployments_by_id = {deployment.id: deployment for deployment in deployments}
        if len(deployments_by_id) != len(deployments) or any(
            deployment.project_id != project_id for deployment in deployments
        ):
            self._uncertain()

        for deployment in deployments:
            try:
                parsed = RuntimeDeployment.from_deployment(deployment)
            except RuntimeConfigurationError:
                self._uncertain()
            generation = deployment.id.hex[:12]
            prefix = f"hm-p{project_id.hex[:12]}"
            resources.append(
                _ExpectedResource("network", "network", f"{prefix}-g{generation}", deployment.id)
            )
            for service in parsed.services:
                resources.extend(
                    (
                        _ExpectedResource(
                            "container",
                            "service",
                            f"{prefix}-{service.name}-g{generation}",
                            deployment.id,
                        ),
                        _ExpectedResource(
                            "image",
                            "image",
                            f"heimdall/{project_id.hex}:g{generation}-{service.name}",
                            deployment.id,
                        ),
                    )
                )

        if len({(item.docker_kind, item.name) for item in resources}) != len(resources):
            self._uncertain()
        if runtime is not None:
            self._validate_runtime_snapshot(runtime, resources, deployments_by_id)
        return tuple(resources)

    def _validate_runtime_snapshot(
        self,
        runtime: ProjectRuntime,
        resources: Sequence[_ExpectedResource],
        deployments_by_id: dict[UUID, Deployment],
    ) -> None:
        if runtime.active_deployment_id is None:
            if (
                runtime.active_network_name is not None
                or runtime.active_container_names
                or runtime.active_image_names
            ):
                self._uncertain()
            return
        if runtime.active_deployment_id not in deployments_by_id:
            self._uncertain()
        active = [item for item in resources if item.deployment_id == runtime.active_deployment_id]
        network_names = {item.name for item in active if item.label_kind == "network"}
        container_names = {item.name for item in active if item.label_kind == "service"}
        image_names = {item.name for item in active if item.label_kind == "image"}
        if (
            {runtime.active_network_name} != network_names
            or set(runtime.active_container_names) != container_names
            or set(runtime.active_image_names) != image_names
        ):
            self._uncertain()

    def _observe_expected(
        self,
        project_id: UUID,
        expected: Sequence[_ExpectedResource],
        heartbeat: Callable[[], bool | None],
    ) -> dict[_ExpectedResource, str | None]:
        states: dict[_ExpectedResource, str | None] = {}
        for resource in expected:
            states[resource] = self._observe_one(project_id, resource, heartbeat)
        return states

    def _observe_one(
        self,
        project_id: UUID,
        resource: _ExpectedResource,
        heartbeat: Callable[[], bool | None],
    ) -> str | None:
        result = self._inspect(resource, heartbeat)
        if result.returncode != 0:
            if _is_not_found(resource, result):
                return None
            self._uncertain(retryable=True)
        try:
            payload = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            self._uncertain()
        labels = payload.get("labels") if isinstance(payload, dict) else None
        resource_id = payload.get("id") if isinstance(payload, dict) else None
        exact = (
            isinstance(resource_id, str)
            and bool(resource_id)
            and isinstance(labels, dict)
            and labels.get("heimdall.managed") == "true"
            and labels.get("heimdall.project-id") == str(project_id)
            and labels.get("heimdall.kind") == resource.label_kind
            and (
                resource.deployment_id is None
                or labels.get("heimdall.deployment-id") == str(resource.deployment_id)
            )
        )
        if not exact:
            self._uncertain()
        return resource_id

    def _reject_extra_resources(
        self,
        project_id: UUID,
        expected: Sequence[_ExpectedResource],
        heartbeat: Callable[[], bool | None],
        *,
        require_empty: bool = False,
    ) -> None:
        expected_names = {
            kind: {item.name for item in expected if item.docker_kind == kind}
            for kind in ("container", "network", "image")
        }
        commands = {
            "container": [
                "ps",
                "--all",
                "--filter",
                f"label=heimdall.project-id={project_id}",
                "--format",
                "{{.Names}}",
            ],
            "network": [
                "network",
                "ls",
                "--filter",
                f"label=heimdall.project-id={project_id}",
                "--format",
                "{{.Name}}",
            ],
            "image": [
                "image",
                "ls",
                "--filter",
                f"label=heimdall.project-id={project_id}",
                "--format",
                "{{.Repository}}:{{.Tag}}",
            ],
        }
        for kind, arguments in commands.items():
            result = self._run_ignored(arguments, heartbeat)
            if result.returncode != 0:
                self._uncertain(retryable=True)
            observed = {line.strip() for line in result.stdout.splitlines() if line.strip()}
            if (require_empty and observed) or not observed.issubset(expected_names[kind]):
                self._uncertain()

    def _inspect(
        self,
        resource: _ExpectedResource,
        heartbeat: Callable[[], bool | None],
    ) -> CommandResult:
        if resource.docker_kind == "network":
            template = '{"id":{{json .Id}},"labels":{{json .Labels}}}'
            arguments = ["network", "inspect", "--format", template, resource.name]
        elif resource.docker_kind == "image":
            template = '{"id":{{json .Id}},"labels":{{json .Config.Labels}}}'
            arguments = ["image", "inspect", "--format", template, resource.name]
        else:
            template = '{"id":{{json .Id}},"labels":{{json .Config.Labels}}}'
            arguments = ["inspect", "--format", template, resource.name]
        return self._run_ignored(arguments, heartbeat)

    def _mutate_remove(
        self,
        resource: _ExpectedResource,
        resource_id: str,
        heartbeat: Callable[[], bool | None],
    ) -> CommandResult:
        if resource.docker_kind == "network":
            arguments = ["network", "rm", resource_id]
        elif resource.docker_kind == "image":
            arguments = ["image", "rm", "--force", resource_id]
        else:
            arguments = ["rm", "--force", resource_id]
        return self._run_ignored(arguments, heartbeat)

    def _run_ignored(
        self,
        arguments: list[str],
        heartbeat: Callable[[], bool | None],
    ) -> CommandResult:
        self._pulse(heartbeat)

        def checked_heartbeat() -> None:
            self._pulse(heartbeat)

        try:
            return self._runner.run(
                [self._docker_executable, *arguments],
                timeout_seconds=self._command_timeout_seconds,
                heartbeat=checked_heartbeat,
                check=False,
            )
        except CommandExecutionError:
            return CommandResult(-1, "")

    def _authorize(
        self,
        mutation_guard: Callable[[], bool],
        heartbeat: Callable[[], bool | None],
    ) -> None:
        self._pulse(heartbeat)
        if not mutation_guard():
            raise RuntimeFailure(
                "DELETION",
                "PROJECT_DELETION_GUARD_REJECTED",
                retryable=True,
                cleanup_candidate=False,
            )

    @staticmethod
    def _pulse(heartbeat: Callable[[], bool | None]) -> None:
        if heartbeat() is False:
            raise RuntimeFailure(
                "DELETION",
                "PROJECT_DELETION_CLAIM_LOST",
                retryable=True,
                cleanup_candidate=False,
            )

    @staticmethod
    def _uncertain(*, retryable: bool = False) -> None:
        raise RuntimeFailure(
            "DELETION",
            "PROJECT_RESOURCES_UNCERTAIN",
            retryable=retryable,
            cleanup_candidate=False,
        )

    @staticmethod
    def _filesystem_uncertain() -> None:
        raise RuntimeFailure(
            "DELETION",
            "PROJECT_FILESYSTEM_RESOURCES_UNCERTAIN",
            cleanup_candidate=False,
        )


def _exact_child_exists(root: Path, basename: str) -> bool:
    try:
        root_fd = _open_private_directory(root)
    except FileNotFoundError:
        return False
    try:
        try:
            os.stat(basename, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True
    finally:
        os.close(root_fd)


def _validate_exact_private_child(root: Path, basename: str) -> None:
    if not basename or "/" in basename or basename in {".", ".."}:
        _raise_filesystem_uncertain()
    try:
        root_fd = _open_private_directory(root)
    except FileNotFoundError:
        return
    except OSError:
        _raise_filesystem_uncertain()
    try:
        try:
            child_fd = os.open(
                basename,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
        except FileNotFoundError:
            return
        except OSError:
            _raise_filesystem_uncertain()
        try:
            _validate_private_tree(child_fd)
        finally:
            os.close(child_fd)
    finally:
        os.close(root_fd)


def _open_private_directory(path: Path) -> int:
    if not path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts[1:]):
        _raise_filesystem_uncertain()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            child_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child_fd
        _validate_private_metadata(os.fstat(fd), directory=True)
    except Exception:
        os.close(fd)
        raise
    return fd


def _validate_private_tree(directory_fd: int) -> None:
    _validate_private_metadata(os.fstat(directory_fd), directory=True)
    for name in os.listdir(directory_fd):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            try:
                _validate_private_tree(child_fd)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(metadata.st_mode):
            _validate_private_metadata(metadata, directory=False)
        else:
            _raise_filesystem_uncertain()


def _validate_private_metadata(metadata: os.stat_result, *, directory: bool) -> None:
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not expected_type(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        _raise_filesystem_uncertain()


def _remove_exact_private_child(
    root: Path,
    basename: str,
    *,
    authorize: Callable[[], None],
) -> None:
    _validate_exact_private_child(root, basename)
    try:
        root_fd = _open_private_directory(root)
    except FileNotFoundError:
        return
    try:
        try:
            child_fd = os.open(
                basename,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
        except FileNotFoundError:
            return
        except OSError:
            _raise_filesystem_uncertain()
        identity = os.fstat(child_fd)
        try:
            _remove_private_tree_contents(child_fd, authorize)
            current = os.stat(basename, dir_fd=root_fd, follow_symlinks=False)
            if not _same_identity(identity, current):
                _raise_filesystem_uncertain()
        finally:
            os.close(child_fd)
        authorize()
        os.rmdir(basename, dir_fd=root_fd)
    except OSError:
        _raise_filesystem_uncertain()
    finally:
        os.close(root_fd)


def _remove_private_tree_contents(directory_fd: int, authorize: Callable[[], None]) -> None:
    for name in os.listdir(directory_fd):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            try:
                identity = os.fstat(child_fd)
                _validate_private_metadata(identity, directory=True)
                _remove_private_tree_contents(child_fd, authorize)
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if not _same_identity(identity, current):
                    _raise_filesystem_uncertain()
            finally:
                os.close(child_fd)
            authorize()
            os.rmdir(name, dir_fd=directory_fd)
        elif stat.S_ISREG(metadata.st_mode):
            file_fd = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            try:
                identity = os.fstat(file_fd)
                _validate_private_metadata(identity, directory=False)
                authorize()
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if not _same_identity(identity, current):
                    _raise_filesystem_uncertain()
                os.unlink(name, dir_fd=directory_fd)
            finally:
                os.close(file_fd)
        else:
            _raise_filesystem_uncertain()


def _raise_filesystem_uncertain() -> None:
    raise RuntimeFailure(
        "DELETION",
        "PROJECT_FILESYSTEM_RESOURCES_UNCERTAIN",
        cleanup_candidate=False,
    )


def _same_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _is_not_found(resource: _ExpectedResource, result: CommandResult) -> bool:
    if result.returncode != 1 or result.stderr_truncated:
        return False
    message = result.stderr.lower()
    if resource.docker_kind == "network":
        return "no such network" in message or ("network" in message and "not found" in message)
    if resource.docker_kind == "image":
        return "no such image" in message
    return "no such object" in message or "no such container" in message
