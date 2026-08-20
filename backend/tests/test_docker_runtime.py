from __future__ import annotations

import json
from copy import deepcopy
from http.client import RemoteDisconnected
from pathlib import Path
from urllib.error import URLError

import pytest
from test_runtime_models import runtime_deployment

from heimdall.deployments.worker import RuntimeFailure
from heimdall.runtime.docker import DockerRuntime, HttpHealthProbe
from heimdall.runtime.models import RuntimeDeployment
from heimdall.runtime.process import CommandExecutionError, CommandResult


class RecordingRunner:
    def __init__(self, managed_deployment_id: str | None = None) -> None:
        self.calls: list[tuple[list[str], bool]] = []
        self.managed_deployment_id = managed_deployment_id

    def run(
        self,
        arguments,
        *,
        timeout_seconds,
        heartbeat=None,
        check=True,
    ) -> CommandResult:
        values = list(arguments)
        self.calls.append((values, check))
        if heartbeat is not None:
            heartbeat()
        if len(values) > 1 and values[1] == "port":
            return CommandResult(0, "127.0.0.1:49152\n")
        if "inspect" in values:
            if self.managed_deployment_id is None:
                return CommandResult(1, "")
            if "{{json .State.Running}}" in values:
                return CommandResult(0, "true")
            return CommandResult(
                0,
                json.dumps(
                    {
                        "heimdall.managed": "true",
                        "heimdall.deployment-id": self.managed_deployment_id,
                    }
                ),
            )
        return CommandResult(0, "")


class FailedBuildRunner(RecordingRunner):
    def run(self, arguments, *, timeout_seconds, heartbeat=None, check=True) -> CommandResult:
        values = list(arguments)
        if len(values) > 1 and values[1] == "build":
            raise CommandExecutionError(
                CommandResult(17, "bounded build output", "bounded build error")
            )
        return super().run(
            values,
            timeout_seconds=timeout_seconds,
            heartbeat=heartbeat,
            check=check,
        )


class ConflictingResourceRunner(RecordingRunner):
    def run(self, arguments, *, timeout_seconds, heartbeat=None, check=True) -> CommandResult:
        values = list(arguments)
        self.calls.append((values, check))
        if "inspect" in values:
            return CommandResult(0, json.dumps({"owner": "external"}))
        return CommandResult(0, "")


class VerifiedCleanupRunner(RecordingRunner):
    def __init__(self, project_id: str, deployment_id: str) -> None:
        super().__init__(deployment_id)
        self.project_id = project_id
        self.removed: set[str] = set()

    def run(self, arguments, *, timeout_seconds, heartbeat=None, check=True) -> CommandResult:
        values = list(arguments)
        self.calls.append((values, check))
        if heartbeat is not None:
            heartbeat()
        if "inspect" in values:
            name = values[-1]
            if name in self.removed:
                return CommandResult(1, "")
            return CommandResult(
                0,
                json.dumps(
                    {
                        "heimdall.managed": "true",
                        "heimdall.project-id": self.project_id,
                        "heimdall.deployment-id": self.managed_deployment_id,
                    }
                ),
            )
        if values[1:3] in (["rm", "--force"], ["network", "rm"], ["image", "rm"]):
            self.removed.add(values[-1])
        return CommandResult(0, "")


class UnavailableCleanupRunner(RecordingRunner):
    def run(self, arguments, *, timeout_seconds, heartbeat=None, check=True) -> CommandResult:
        values = list(arguments)
        self.calls.append((values, check))
        if "inspect" in values:
            raise CommandExecutionError(-1)
        return CommandResult(0, "")


class RecordingProbe:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def wait_until_healthy(self, url, *, timeout_seconds, heartbeat) -> None:
        self.urls.append(url)
        heartbeat()


class RecordingProgress:
    def __init__(self) -> None:
        self.stages: list[str] = []
        self.heartbeats = 0

    def heartbeat(self) -> None:
        self.heartbeats += 1

    def stage(self, status, code, message) -> None:
        assert code and message
        self.stages.append(status.value)


class FilePaths:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.paths: dict[str, Path] = {}

    def add(self, reference: str, value: str) -> None:
        path = self.root / f"secret-{len(self.paths)}"
        path.write_text(value)
        path.chmod(0o400)
        self.paths[reference] = path

    def resolve(self, reference: str, fingerprint: str) -> Path:
        assert len(fingerprint) == 64
        return self.paths[reference]

    def read(self, reference: str, fingerprint: str) -> str:
        return self.resolve(reference, fingerprint).read_text()

    def create(self, reference_root: str, version: int, value: str | None = None):
        raise NotImplementedError


def test_docker_candidate_uses_file_mounts_and_service_scoped_managed_values(
    tmp_path: Path,
) -> None:
    item = runtime_deployment()
    runtime = RuntimeDeployment.from_deployment(item)
    source = tmp_path / "source"
    source.mkdir()
    (source / "Dockerfile").write_text("FROM scratch\n")
    secrets = FilePaths(tmp_path)
    user_secret = runtime.services[0].secrets[0]
    database = runtime.database
    assert database is not None
    secrets.add(user_secret.reference, "user-secret-canary")
    secrets.add(database.credential_reference, "database-secret-canary")
    runner = RecordingRunner()
    probe = RecordingProbe()
    progress = RecordingProgress()

    candidate = DockerRuntime(runner, probe).start_candidate(
        item, runtime, source, secrets, progress
    )

    commands = [call[0] for call in runner.calls]
    create_command = next(command for command in commands if command[1] == "create")
    command_text = " ".join(create_command)
    assert "APP_ENV=production" in create_command
    assert "JWT_SECRET=/run/secrets/heimdall/environment/jwt_secret" in create_command
    assert (
        "DATABASE_PASSWORD_FILE=/run/secrets/heimdall/project-database-password" in create_command
    )
    assert "DATABASE_HOST=managed-db.internal" in create_command
    assert "user-secret-canary" not in command_text
    assert "database-secret-canary" not in command_text
    assert not any(command[1:3] == ["network", "connect"] for command in commands)
    assert candidate.services[0].health_port == 49152
    assert probe.urls == ["http://127.0.0.1:49152/health"]
    assert progress.stages == ["BUILDING", "STARTING", "HEALTH_CHECKING"]


def test_failed_docker_command_preserves_bounded_output_without_raw_arguments(
    tmp_path: Path,
) -> None:
    item = runtime_deployment()
    runtime = RuntimeDeployment.from_deployment(item)
    source = tmp_path / "source"
    source.mkdir()
    (source / "Dockerfile").write_text("FROM scratch\n")

    with pytest.raises(RuntimeFailure) as raised:
        DockerRuntime(FailedBuildRunner(), RecordingProbe()).start_candidate(
            item,
            runtime,
            source,
            FilePaths(tmp_path),
            RecordingProgress(),
        )

    assert raised.value.code == "IMAGE_BUILD_FAILED"
    assert raised.value.command_output is not None
    assert raised.value.command_output.operation == "DOCKER_BUILD"
    assert raised.value.command_output.return_code == 17
    assert raised.value.command_output.stdout == "bounded build output"
    assert raised.value.command_output.stderr == "bounded build error"


def test_docker_candidate_uses_configured_host_for_health_probe(tmp_path: Path) -> None:
    item = runtime_deployment()
    runtime = RuntimeDeployment.from_deployment(item)
    source = tmp_path / "source"
    source.mkdir()
    (source / "Dockerfile").write_text("FROM scratch\n")
    secrets = FilePaths(tmp_path)
    database = runtime.database
    assert database is not None
    secrets.add(runtime.services[0].secrets[0].reference, "user-secret-canary")
    secrets.add(database.credential_reference, "database-secret-canary")
    probe = RecordingProbe()

    DockerRuntime(RecordingRunner(), probe, probe_host="host.docker.internal").start_candidate(
        item, runtime, source, secrets, RecordingProgress()
    )

    assert probe.urls == ["http://host.docker.internal:49152/health"]


def test_candidate_creates_all_containers_and_retries_start_before_port_lookup(
    tmp_path: Path,
) -> None:
    item = runtime_deployment()
    snapshot = deepcopy(item.config_snapshot)
    snapshot["services"].insert(
        0,
        {
            "name": "web",
            "build": {"context": ".", "dockerfile": "Dockerfile"},
            "internalPort": 80,
            "healthPath": "/",
            "projectDatabaseAccess": False,
            "environment": [],
        },
    )
    snapshot["routes"] = [
        {"path": "/", "service": "web"},
        {"path": "/api", "service": "api"},
    ]
    object.__setattr__(item, "config_snapshot", snapshot)
    runtime = RuntimeDeployment.from_deployment(item)
    source = tmp_path / "source"
    source.mkdir()
    (source / "Dockerfile").write_text("FROM scratch\n")
    secrets = FilePaths(tmp_path)
    user_secret = runtime.services[1].secrets[0]
    database = runtime.database
    assert database is not None
    secrets.add(user_secret.reference, "user-secret-canary")
    secrets.add(database.credential_reference, "database-secret-canary")
    runner = RecordingRunner()

    DockerRuntime(runner, RecordingProbe()).start_candidate(
        item, runtime, source, secrets, RecordingProgress()
    )

    commands = [call[0] for call in runner.calls]
    lifecycle = [command[1] for command in commands if command[1] in {"create", "start", "port"}]
    assert lifecycle == [
        "create",
        "create",
        "start",
        "start",
        "start",
        "start",
        "port",
        "port",
    ]


def test_candidate_cleanup_targets_only_deterministic_deployment_resources(tmp_path: Path) -> None:
    item = runtime_deployment()
    runtime = RuntimeDeployment.from_deployment(item)
    runner = RecordingRunner(str(item.id))

    DockerRuntime(runner, RecordingProbe()).cleanup_candidate(item, runtime)

    commands = [call[0] for call in runner.calls]
    mutations = [command for command in commands if "inspect" not in command]
    assert mutations[0][1:3] == ["rm", "--force"]
    assert mutations[1][1:3] == ["network", "rm"]
    assert mutations[2][1:4] == ["image", "rm", "--force"]
    assert all(check is False for _, check in runner.calls)


def test_verified_cleanup_removes_only_exact_project_deployment_resources() -> None:
    item = runtime_deployment()
    runtime = RuntimeDeployment.from_deployment(item)
    runner = VerifiedCleanupRunner(str(item.project_id), str(item.id))

    DockerRuntime(runner, RecordingProbe()).cleanup_candidate_verified(
        item,
        runtime,
        RecordingProgress(),
    )

    mutations = [command for command, _ in runner.calls if "inspect" not in command]
    assert [command[1:3] for command in mutations] == [
        ["rm", "--force"],
        ["network", "rm"],
        ["image", "rm"],
    ]


def test_verified_cleanup_does_not_mutate_a_conflicting_resource_name() -> None:
    item = runtime_deployment()
    runtime = RuntimeDeployment.from_deployment(item)
    runner = ConflictingResourceRunner()

    with pytest.raises(RuntimeFailure) as raised:
        DockerRuntime(runner, RecordingProbe()).cleanup_candidate_verified(
            item,
            runtime,
            RecordingProgress(),
        )

    assert raised.value.code == "CANDIDATE_RESOURCE_NAME_CONFLICT"
    assert all("inspect" in command for command, _ in runner.calls)


def test_verified_cleanup_preserves_resources_when_docker_cannot_be_observed() -> None:
    item = runtime_deployment()
    runtime = RuntimeDeployment.from_deployment(item)
    runner = UnavailableCleanupRunner()

    with pytest.raises(RuntimeFailure) as raised:
        DockerRuntime(runner, RecordingProbe()).cleanup_candidate_verified(
            item,
            runtime,
            RecordingProgress(),
        )

    assert raised.value.code == "CANDIDATE_RESOURCE_OBSERVATION_FAILED"
    assert all("inspect" in command for command, _ in runner.calls)


def test_existing_candidate_is_observed_only_when_all_exact_resources_are_running() -> None:
    item = runtime_deployment()
    runtime = RuntimeDeployment.from_deployment(item)
    runner = RecordingRunner(str(item.id))

    observed = DockerRuntime(runner, RecordingProbe()).observe_candidate(
        item, runtime, RecordingProgress()
    )

    assert observed is not None
    assert observed.network_name == f"hm-p{item.project_id.hex[:12]}-g{item.id.hex[:12]}"
    assert observed.services[0].container_name.endswith(f"-g{item.id.hex[:12]}")
    assert observed.services[0].health_port == 49152


def test_incomplete_candidate_is_not_treated_as_recoverable() -> None:
    item = runtime_deployment()
    runtime = RuntimeDeployment.from_deployment(item)

    observed = DockerRuntime(RecordingRunner(), RecordingProbe()).observe_candidate(
        item, runtime, RecordingProgress()
    )

    assert observed is None


def test_health_probe_retries_when_service_closes_connection_during_startup(monkeypatch) -> None:
    attempts = 0

    class HealthyResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    def open_after_startup(request, timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RemoteDisconnected("service is still starting")
        return HealthyResponse()

    monkeypatch.setattr("heimdall.runtime.docker.urlopen", open_after_startup)

    HttpHealthProbe(interval_seconds=0).wait_until_healthy(
        "http://127.0.0.1:49152/health",
        timeout_seconds=1,
        heartbeat=lambda: None,
    )

    assert attempts == 2


def test_health_probe_returns_stable_timeout_failure(monkeypatch) -> None:
    def unavailable(request, timeout):
        raise URLError("not ready")

    monkeypatch.setattr("heimdall.runtime.docker.urlopen", unavailable)

    with pytest.raises(RuntimeFailure) as raised:
        HttpHealthProbe(interval_seconds=0).wait_until_healthy(
            "http://127.0.0.1:49152/health",
            timeout_seconds=0.001,
            heartbeat=lambda: None,
        )

    assert raised.value.stage == "HEALTH_CHECK"
    assert raised.value.code == "SERVICE_HEALTH_TIMEOUT"


def test_build_command_failure_is_non_retryable_and_has_stable_code(tmp_path: Path) -> None:
    item = runtime_deployment()
    runtime = RuntimeDeployment.from_deployment(item)
    source = tmp_path / "source"
    source.mkdir()
    (source / "Dockerfile").write_text("FROM scratch\n")

    with pytest.raises(RuntimeFailure) as raised:
        DockerRuntime(FailedBuildRunner(), RecordingProbe()).start_candidate(
            item,
            runtime,
            source,
            FilePaths(tmp_path),
            RecordingProgress(),
        )

    assert raised.value.stage == "BUILD"
    assert raised.value.code == "IMAGE_BUILD_FAILED"
    assert raised.value.retryable is False


def test_unmanaged_name_collision_is_not_deleted_or_overwritten(tmp_path: Path) -> None:
    item = runtime_deployment()
    runtime = RuntimeDeployment.from_deployment(item)
    source = tmp_path / "source"
    source.mkdir()
    (source / "Dockerfile").write_text("FROM scratch\n")
    runner = ConflictingResourceRunner()

    with pytest.raises(RuntimeFailure) as raised:
        DockerRuntime(runner, RecordingProbe()).start_candidate(
            item,
            runtime,
            source,
            FilePaths(tmp_path),
            RecordingProgress(),
        )

    assert raised.value.code == "RESOURCE_NAME_CONFLICT"
    assert not any(call[0][1] in {"rm", "build"} for call in runner.calls)
