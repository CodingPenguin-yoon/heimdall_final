from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from heimdall.deployments.worker import RuntimeFailure
from heimdall.runtime.gateway_identity import project_gateway_alias, project_gateway_name
from heimdall.runtime.process import CommandExecutionError, CommandResult, CommandRunner

_DOCKER_RESOURCE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")


class EdgeNetworkConnector(Protocol):
    def ensure_gateway_attached(
        self,
        project_id: UUID,
        *,
        heartbeat: Callable[[], None],
    ) -> None: ...


class DockerEdgeNetworkConnector:
    def __init__(
        self,
        runner: CommandRunner,
        *,
        executable: str = "docker",
        network_name: str = "heimdall-edge",
        command_timeout_seconds: float = 120,
    ) -> None:
        if not _DOCKER_RESOURCE_NAME.fullmatch(network_name):
            raise ValueError("edge network name must be a valid Docker resource name")
        if command_timeout_seconds <= 0:
            raise ValueError("edge network command timeout must be positive")
        self._runner = runner
        self._executable = executable
        self._network_name = network_name
        self._command_timeout_seconds = command_timeout_seconds

    def ensure_gateway_attached(
        self,
        project_id: UUID,
        *,
        heartbeat: Callable[[], None],
    ) -> None:
        gateway_name = project_gateway_name(project_id)
        alias = project_gateway_alias(project_id)
        network = self._run_ignored(
            ["network", "inspect", "--format", "{{json .Labels}}", self._network_name],
            heartbeat=heartbeat,
        )
        if network.returncode != 0:
            raise RuntimeFailure(
                "ACTIVATION",
                "EDGE_NETWORK_UNAVAILABLE",
                retryable=True,
            )
        if not _has_labels(
            network.stdout,
            {
                "heimdall.managed": "true",
                "heimdall.kind": "edge-network",
            },
        ):
            raise RuntimeFailure("ACTIVATION", "EDGE_NETWORK_NAME_CONFLICT")

        gateway = self._run_ignored(
            [
                "inspect",
                "--format",
                '{"labels":{{json .Config.Labels}},"running":{{json .State.Running}}}',
                gateway_name,
            ],
            heartbeat=heartbeat,
        )
        if gateway.returncode != 0:
            raise RuntimeFailure(
                "ACTIVATION",
                "GATEWAY_START_FAILED",
                retryable=True,
            )
        try:
            observation = json.loads(gateway.stdout)
        except (json.JSONDecodeError, TypeError) as error:
            raise RuntimeFailure("ACTIVATION", "GATEWAY_NAME_CONFLICT") from error
        if not isinstance(observation, dict) or not _matches_labels(
            observation.get("labels"),
            {
                "heimdall.managed": "true",
                "heimdall.project-id": str(project_id),
                "heimdall.kind": "gateway",
            },
        ):
            raise RuntimeFailure("ACTIVATION", "GATEWAY_NAME_CONFLICT")
        if not isinstance(observation.get("running"), bool):
            raise RuntimeFailure("ACTIVATION", "GATEWAY_NAME_CONFLICT")
        if observation["running"] is not True:
            raise RuntimeFailure(
                "ACTIVATION",
                "GATEWAY_START_FAILED",
                retryable=True,
            )

        self._run_ignored(
            [
                "network",
                "connect",
                "--alias",
                alias,
                self._network_name,
                gateway_name,
            ],
            heartbeat=heartbeat,
        )
        attached = self._run_ignored(
            ["inspect", "--format", "{{json .NetworkSettings.Networks}}", gateway_name],
            heartbeat=heartbeat,
        )
        if attached.returncode != 0 or not _has_network_alias(
            attached.stdout,
            self._network_name,
            alias,
        ):
            raise RuntimeFailure(
                "ACTIVATION",
                "GATEWAY_EDGE_NETWORK_CONNECT_FAILED",
                retryable=True,
            )

    def _run_ignored(
        self,
        arguments: list[str],
        *,
        heartbeat: Callable[[], None],
    ) -> CommandResult:
        try:
            return self._runner.run(
                [self._executable, *arguments],
                timeout_seconds=self._command_timeout_seconds,
                heartbeat=heartbeat,
                check=False,
            )
        except CommandExecutionError:
            return CommandResult(-1, "")


def _has_labels(output: str, expected: dict[str, str]) -> bool:
    try:
        labels = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return False
    return _matches_labels(labels, expected)


def _matches_labels(labels: object, expected: dict[str, str]) -> bool:
    return isinstance(labels, dict) and all(
        labels.get(name) == value for name, value in expected.items()
    )


def _has_network_alias(output: str, network_name: str, alias: str) -> bool:
    try:
        networks = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(networks, dict):
        return False
    network = networks.get(network_name)
    if not isinstance(network, dict):
        return False
    aliases = network.get("Aliases")
    return isinstance(aliases, list) and alias in aliases
