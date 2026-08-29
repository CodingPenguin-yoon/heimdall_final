from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_runtime_models import runtime_deployment

import heimdall.runtime.project_teardown as project_teardown_module
from heimdall.deployments.worker import RuntimeFailure
from heimdall.runtime.gateway_identity import project_gateway_name
from heimdall.runtime.process import CommandResult
from heimdall.runtime.project_teardown import ProjectRuntimeTeardown
from heimdall.runtime.repository import ProjectRuntime


class ExactProjectResourcesRunner:
    def __init__(self, project_id: str, deployment_id: str, expected: dict[str, dict[str, str]]):
        self.project_id = project_id
        self.deployment_id = deployment_id
        self.resources = dict(expected)
        self.ids = {name: f"docker-id:{name}" for name in expected}
        self.calls: list[list[str]] = []

    def run(self, arguments, *, timeout_seconds, heartbeat=None, check=True) -> CommandResult:
        values = list(arguments)
        self.calls.append(values)
        if heartbeat is not None:
            heartbeat()
        if values[1:3] == ["ps", "--all"]:
            names = [
                name
                for name, labels in self.resources.items()
                if labels["kind"] in {"gateway", "service"}
            ]
            return CommandResult(0, "\n".join(names))
        if values[1:3] == ["network", "ls"]:
            names = [name for name, labels in self.resources.items() if labels["kind"] == "network"]
            return CommandResult(0, "\n".join(names))
        if values[1:3] == ["image", "ls"]:
            names = [name for name, labels in self.resources.items() if labels["kind"] == "image"]
            return CommandResult(0, "\n".join(names))
        if "inspect" in values:
            name = values[-1]
            labels = self.resources.get(name)
            if labels is None:
                kind = values[1] if values[1] in {"network", "image"} else "object"
                return CommandResult(1, "", f"Error: No such {kind}: {name}")
            payload = {
                "heimdall.managed": "true",
                "heimdall.project-id": self.project_id,
                "heimdall.kind": labels["kind"],
            }
            if labels["kind"] != "gateway":
                payload["heimdall.deployment-id"] = self.deployment_id
            return CommandResult(
                0,
                json.dumps({"id": self.ids[name], "labels": payload}),
            )
        if values[1:3] in (["rm", "--force"], ["network", "rm"], ["image", "rm"]):
            removed_id = values[-1]
            removed_name = next(
                (name for name, resource_id in self.ids.items() if resource_id == removed_id),
                None,
            )
            if removed_name is not None:
                self.resources.pop(removed_name, None)
        return CommandResult(0, "")


def test_teardown_removes_only_db_derived_resources_in_safe_order(tmp_path: Path) -> None:
    deployment = runtime_deployment()
    service_name = deployment.config_snapshot["services"][0]["name"]
    generation = deployment.id.hex[:12]
    prefix = f"hm-p{deployment.project_id.hex[:12]}"
    gateway = project_gateway_name(deployment.project_id)
    container = f"{prefix}-{service_name}-g{generation}"
    network = f"{prefix}-g{generation}"
    image = f"heimdall/{deployment.project_id.hex}:g{generation}-{service_name}"
    expected = {
        gateway: {"kind": "gateway"},
        container: {"kind": "service"},
        network: {"kind": "network"},
        image: {"kind": "image"},
    }
    runner = ExactProjectResourcesRunner(str(deployment.project_id), str(deployment.id), expected)
    workspace_root = tmp_path / "workspaces"
    gateway_root = tmp_path / "gateways"
    workspace_root.mkdir(mode=0o700)
    gateway_root.mkdir(mode=0o700)
    workspace = workspace_root / deployment.id.hex
    gateway_config = gateway_root / deployment.project_id.hex
    other_workspace = workspace_root / ("0" * 32)
    other_gateway_config = gateway_root / ("f" * 32)
    workspace.mkdir(mode=0o700)
    gateway_config.mkdir(mode=0o700)
    other_workspace.mkdir(mode=0o700)
    other_gateway_config.mkdir(mode=0o700)
    source_file = workspace / "source.py"
    config_file = gateway_config / "current.conf"
    source_file.write_text("pass\n")
    config_file.write_text("server {}\n")
    source_file.chmod(0o600)
    config_file.chmod(0o600)
    runtime = ProjectRuntime(
        project_id=deployment.project_id,
        gateway_container_name=gateway,
        preview_port=49152,
        active_deployment_id=deployment.id,
        active_network_name=network,
        active_container_names=(container,),
        active_image_names=(image,),
        updated_at=datetime.now(UTC),
    )
    guard_calls = 0

    def guard() -> bool:
        nonlocal guard_calls
        guard_calls += 1
        return True

    ProjectRuntimeTeardown(runner, workspace_root, gateway_root).teardown(
        deployment.project_id,
        (deployment,),
        runtime,
        mutation_guard=guard,
        heartbeat=lambda: True,
    )

    mutations = [
        command
        for command in runner.calls
        if command[1:3] in (["rm", "--force"], ["network", "rm"], ["image", "rm"])
    ]
    assert [command[-1] for command in mutations] == [
        runner.ids[gateway],
        runner.ids[container],
        runner.ids[network],
        runner.ids[image],
    ]
    assert guard_calls == 8
    assert not workspace.exists()
    assert not gateway_config.exists()
    assert other_workspace.is_dir()
    assert other_gateway_config.is_dir()
    command_text = "\n".join(" ".join(command) for command in runner.calls)
    assert "heimdall-edge" not in command_text
    assert "nginx:1.29-alpine" not in command_text

    runner.calls.clear()
    ProjectRuntimeTeardown(runner, workspace_root, gateway_root).verify_absent(
        deployment.project_id,
        (deployment,),
        runtime,
        mutation_guard=lambda: True,
        heartbeat=lambda: True,
    )
    assert not any("rm" in command for command in runner.calls)


class FailedObservationRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, arguments, *, timeout_seconds, heartbeat=None, check=True) -> CommandResult:
        values = list(arguments)
        self.calls.append(values)
        if "inspect" in values:
            return CommandResult(125, "", "docker daemon unavailable")
        return CommandResult(0, "")


def test_teardown_preserves_everything_when_expected_resource_observation_fails(
    tmp_path: Path,
) -> None:
    deployment = runtime_deployment()
    runner = FailedObservationRunner()

    with pytest.raises(RuntimeFailure) as raised:
        ProjectRuntimeTeardown(
            runner,
            tmp_path / "missing-workspaces",
            tmp_path / "missing-gateways",
        ).teardown(
            deployment.project_id,
            (deployment,),
            None,
            mutation_guard=lambda: True,
            heartbeat=lambda: True,
        )

    assert raised.value.code == "PROJECT_RESOURCES_UNCERTAIN"
    assert not any(
        command[1:3] in (["rm", "--force"], ["network", "rm"], ["image", "rm"])
        for command in runner.calls
    )


class MissingKindRunner(ExactProjectResourcesRunner):
    def run(self, arguments, *, timeout_seconds, heartbeat=None, check=True) -> CommandResult:
        result = super().run(
            arguments,
            timeout_seconds=timeout_seconds,
            heartbeat=heartbeat,
            check=check,
        )
        values = list(arguments)
        if "inspect" not in values or result.returncode != 0:
            return result
        payload = json.loads(result.stdout)
        labels = payload["labels"]
        labels.pop("heimdall.kind", None)
        return CommandResult(0, json.dumps(payload))


def test_teardown_preserves_legacy_resources_without_exact_kind_label(tmp_path: Path) -> None:
    deployment = runtime_deployment()
    generation = deployment.id.hex[:12]
    service_name = deployment.config_snapshot["services"][0]["name"]
    container = f"hm-p{deployment.project_id.hex[:12]}-{service_name}-g{generation}"
    runner = MissingKindRunner(
        str(deployment.project_id),
        str(deployment.id),
        {container: {"kind": "service"}},
    )

    with pytest.raises(RuntimeFailure) as raised:
        ProjectRuntimeTeardown(runner, tmp_path / "workspaces", tmp_path / "gateways").teardown(
            deployment.project_id,
            (deployment,),
            None,
            mutation_guard=lambda: True,
            heartbeat=lambda: True,
        )

    assert raised.value.code == "PROJECT_RESOURCES_UNCERTAIN"
    assert container in runner.resources


def test_teardown_preserves_all_resources_when_project_has_an_unexpected_resource(
    tmp_path: Path,
) -> None:
    deployment = runtime_deployment()
    extra = f"hm-p{deployment.project_id.hex[:12]}-unexpected"
    runner = ExactProjectResourcesRunner(
        str(deployment.project_id),
        str(deployment.id),
        {extra: {"kind": "service"}},
    )

    with pytest.raises(RuntimeFailure) as raised:
        ProjectRuntimeTeardown(runner, tmp_path / "workspaces", tmp_path / "gateways").teardown(
            deployment.project_id,
            (deployment,),
            None,
            mutation_guard=lambda: True,
            heartbeat=lambda: True,
        )

    assert raised.value.code == "PROJECT_RESOURCES_UNCERTAIN"
    assert extra in runner.resources


def test_teardown_rechecks_deletion_and_route_guard_before_each_mutation(tmp_path: Path) -> None:
    deployment = runtime_deployment()
    generation = deployment.id.hex[:12]
    service_name = deployment.config_snapshot["services"][0]["name"]
    prefix = f"hm-p{deployment.project_id.hex[:12]}"
    container = f"{prefix}-{service_name}-g{generation}"
    network = f"{prefix}-g{generation}"
    runner = ExactProjectResourcesRunner(
        str(deployment.project_id),
        str(deployment.id),
        {
            container: {"kind": "service"},
            network: {"kind": "network"},
        },
    )
    guard_results = iter((True, False))

    with pytest.raises(RuntimeFailure) as raised:
        ProjectRuntimeTeardown(runner, tmp_path / "workspaces", tmp_path / "gateways").teardown(
            deployment.project_id,
            (deployment,),
            None,
            mutation_guard=lambda: next(guard_results),
            heartbeat=lambda: True,
        )

    assert raised.value.code == "PROJECT_DELETION_GUARD_REJECTED"
    assert container not in runner.resources
    assert network in runner.resources


def test_teardown_preflights_filesystem_identity_before_docker_mutation(tmp_path: Path) -> None:
    deployment = runtime_deployment()
    generation = deployment.id.hex[:12]
    service_name = deployment.config_snapshot["services"][0]["name"]
    container = f"hm-p{deployment.project_id.hex[:12]}-{service_name}-g{generation}"
    runner = ExactProjectResourcesRunner(
        str(deployment.project_id),
        str(deployment.id),
        {container: {"kind": "service"}},
    )
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (workspace_root / deployment.id.hex).symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeFailure) as raised:
        ProjectRuntimeTeardown(
            runner,
            workspace_root,
            tmp_path / "gateways",
        ).teardown(
            deployment.project_id,
            (deployment,),
            None,
            mutation_guard=lambda: True,
            heartbeat=lambda: True,
        )

    assert raised.value.code == "PROJECT_FILESYSTEM_RESOURCES_UNCERTAIN"
    assert container in runner.resources
    assert outside.exists()


def test_teardown_rechecks_docker_absence_after_filesystem_cleanup(tmp_path: Path) -> None:
    deployment = runtime_deployment()
    generation = deployment.id.hex[:12]
    service_name = deployment.config_snapshot["services"][0]["name"]
    container = f"hm-p{deployment.project_id.hex[:12]}-{service_name}-g{generation}"
    labels = {"kind": "service"}
    runner = ExactProjectResourcesRunner(
        str(deployment.project_id),
        str(deployment.id),
        {container: labels},
    )
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir(mode=0o700)
    workspace = workspace_root / deployment.id.hex
    workspace.mkdir(mode=0o700)
    source = workspace / "source.py"
    source.write_text("pass\n")
    source.chmod(0o600)
    guard_calls = 0

    def guard() -> bool:
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls == 2:
            runner.resources[container] = labels
        return True

    with pytest.raises(RuntimeFailure) as raised:
        ProjectRuntimeTeardown(
            runner,
            workspace_root,
            tmp_path / "gateways",
        ).teardown(
            deployment.project_id,
            (deployment,),
            None,
            mutation_guard=guard,
            heartbeat=lambda: True,
        )

    assert raised.value.code == "PROJECT_RESOURCES_UNCERTAIN"
    assert container in runner.resources


def test_teardown_rejects_a_symlinked_workspace_root_without_following_it(tmp_path: Path) -> None:
    deployment = runtime_deployment()
    real_root = tmp_path / "real-workspaces"
    real_root.mkdir(mode=0o700)
    workspace = real_root / deployment.id.hex
    workspace.mkdir(mode=0o700)
    marker = workspace / "marker"
    marker.write_text("preserve")
    marker.chmod(0o600)
    linked_root = tmp_path / "workspaces"
    linked_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(RuntimeFailure) as raised:
        ProjectRuntimeTeardown(
            FailedObservationRunnerWithoutInspectFailure(),
            linked_root,
            tmp_path / "gateways",
        ).teardown(
            deployment.project_id,
            (deployment,),
            None,
            mutation_guard=lambda: True,
            heartbeat=lambda: True,
        )

    assert raised.value.code == "PROJECT_FILESYSTEM_RESOURCES_UNCERTAIN"
    assert marker.read_text() == "preserve"


class FailedObservationRunnerWithoutInspectFailure:
    def run(self, arguments, *, timeout_seconds, heartbeat=None, check=True) -> CommandResult:
        if "inspect" in arguments:
            kind = arguments[1] if arguments[1] in {"network", "image"} else "object"
            return CommandResult(1, "", f"Error: No such {kind}: expected")
        return CommandResult(0, "")


class UnclassifiedExitOneRunner:
    def run(self, arguments, *, timeout_seconds, heartbeat=None, check=True) -> CommandResult:
        return CommandResult(1 if "inspect" in arguments else 0, "")


def test_teardown_does_not_treat_unclassified_inspect_exit_one_as_absent(tmp_path: Path) -> None:
    deployment = runtime_deployment()

    with pytest.raises(RuntimeFailure) as raised:
        ProjectRuntimeTeardown(
            UnclassifiedExitOneRunner(),
            tmp_path / "workspaces",
            tmp_path / "gateways",
        ).teardown(
            deployment.project_id,
            (deployment,),
            None,
            mutation_guard=lambda: True,
            heartbeat=lambda: True,
        )

    assert raised.value.code == "PROJECT_RESOURCES_UNCERTAIN"


class ReplacedBeforeMutationRunner(ExactProjectResourcesRunner):
    def __init__(self, project_id: str, deployment_id: str, expected):
        super().__init__(project_id, deployment_id, expected)
        self.inspect_count = 0

    def run(self, arguments, *, timeout_seconds, heartbeat=None, check=True) -> CommandResult:
        result = super().run(
            arguments,
            timeout_seconds=timeout_seconds,
            heartbeat=heartbeat,
            check=check,
        )
        if "inspect" in arguments and result.returncode == 0:
            self.inspect_count += 1
            if self.inspect_count >= 2:
                payload = json.loads(result.stdout)
                labels = payload["labels"]
                labels["heimdall.project-id"] = "00000000-0000-0000-0000-000000000000"
                return CommandResult(0, json.dumps(payload))
        return result


def test_teardown_revalidates_immutable_identity_immediately_before_mutation(
    tmp_path: Path,
) -> None:
    deployment = runtime_deployment()
    generation = deployment.id.hex[:12]
    service_name = deployment.config_snapshot["services"][0]["name"]
    container = f"hm-p{deployment.project_id.hex[:12]}-{service_name}-g{generation}"
    runner = ReplacedBeforeMutationRunner(
        str(deployment.project_id),
        str(deployment.id),
        {container: {"kind": "service"}},
    )

    with pytest.raises(RuntimeFailure) as raised:
        ProjectRuntimeTeardown(runner, tmp_path / "workspaces", tmp_path / "gateways").teardown(
            deployment.project_id,
            (deployment,),
            None,
            mutation_guard=lambda: True,
            heartbeat=lambda: True,
        )

    assert raised.value.code == "PROJECT_RESOURCES_UNCERTAIN"
    assert container in runner.resources


class ListedAfterNotFoundRunner(ExactProjectResourcesRunner):
    def run(self, arguments, *, timeout_seconds, heartbeat=None, check=True) -> CommandResult:
        values = list(arguments)
        if "inspect" in values:
            return CommandResult(1, "", "Error: No such object: expected")
        return super().run(
            arguments,
            timeout_seconds=timeout_seconds,
            heartbeat=heartbeat,
            check=check,
        )


def test_teardown_requires_final_project_label_listing_to_be_empty(tmp_path: Path) -> None:
    deployment = runtime_deployment()
    generation = deployment.id.hex[:12]
    service_name = deployment.config_snapshot["services"][0]["name"]
    container = f"hm-p{deployment.project_id.hex[:12]}-{service_name}-g{generation}"
    runner = ListedAfterNotFoundRunner(
        str(deployment.project_id),
        str(deployment.id),
        {container: {"kind": "service"}},
    )

    with pytest.raises(RuntimeFailure) as raised:
        ProjectRuntimeTeardown(runner, tmp_path / "workspaces", tmp_path / "gateways").teardown(
            deployment.project_id,
            (deployment,),
            None,
            mutation_guard=lambda: True,
            heartbeat=lambda: True,
        )

    assert raised.value.code == "PROJECT_RESOURCES_UNCERTAIN"


def test_teardown_rejects_workspace_inode_replacement_before_rmdir(
    tmp_path: Path, monkeypatch
) -> None:
    deployment = runtime_deployment()
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir(mode=0o700)
    workspace = workspace_root / deployment.id.hex
    workspace.mkdir(mode=0o700)
    source = workspace / "source.py"
    source.write_text("pass\n")
    source.chmod(0o600)
    original_remove = project_teardown_module._remove_private_tree_contents
    original_stat = project_teardown_module.os.stat
    removed_contents = False

    def finish_remove(descriptor: int, authorize) -> None:
        nonlocal removed_contents
        original_remove(descriptor, authorize)
        removed_contents = True

    def replaced_stat(path, *args, **kwargs):
        metadata = original_stat(path, *args, **kwargs)
        if removed_contents and path == deployment.id.hex and kwargs.get("dir_fd") is not None:
            values = list(metadata)
            values[1] += 1
            return project_teardown_module.os.stat_result(values)
        return metadata

    monkeypatch.setattr(project_teardown_module, "_remove_private_tree_contents", finish_remove)
    monkeypatch.setattr(project_teardown_module.os, "stat", replaced_stat)

    with pytest.raises(RuntimeFailure) as raised:
        ProjectRuntimeTeardown(
            FailedObservationRunnerWithoutInspectFailure(),
            workspace_root,
            tmp_path / "gateways",
        ).teardown(
            deployment.project_id,
            (deployment,),
            None,
            mutation_guard=lambda: True,
            heartbeat=lambda: True,
        )

    assert raised.value.code == "PROJECT_FILESYSTEM_RESOURCES_UNCERTAIN"
    assert workspace.is_dir()
