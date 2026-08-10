from __future__ import annotations

import json
import sys

import pytest
from test_runtime_models import runtime_deployment

from heimdall.runtime.docker import DockerServiceLogReader
from heimdall.runtime.logs import SERVICE_LOG_MAX_LINE_BYTES, SERVICE_LOG_TAIL, ServiceLogError
from heimdall.runtime.process import CommandResult, SubprocessCommandRunner
from heimdall.secrets.store import SecretStoreError


class LogRunner:
    def __init__(
        self,
        *,
        project_id: str,
        deployment_id: str,
        stdout: str = "",
        stderr: str = "",
        labels: dict[str, str] | None = None,
        logs_returncode: int = 0,
    ) -> None:
        self.calls: list[list[str]] = []
        self.stdout = stdout
        self.stderr = stderr
        self.labels = labels or {
            "heimdall.managed": "true",
            "heimdall.project-id": project_id,
            "heimdall.deployment-id": deployment_id,
        }
        self.container_id = "f" * 64
        self.logs_returncode = logs_returncode

    def run(self, arguments, *, timeout_seconds, heartbeat=None, check=True) -> CommandResult:
        values = list(arguments)
        self.calls.append(values)
        if values[1] == "inspect":
            return CommandResult(
                0,
                f"{json.dumps(self.container_id)} {json.dumps(self.labels)}",
            )
        if values[1] == "logs":
            return CommandResult(self.logs_returncode, self.stdout, self.stderr)
        raise AssertionError(values)


class LogSecrets:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def read(self, reference: str, fingerprint: str) -> str:
        if reference not in self.values:
            raise SecretStoreError("missing")
        assert len(fingerprint) == 64
        return self.values[reference]

    def create(self, reference_root, version, value=None):
        raise NotImplementedError

    def resolve(self, reference, fingerprint):
        raise NotImplementedError


def _secrets(item) -> LogSecrets:
    service = item.config_snapshot["services"][0]
    database = item.config_snapshot["managedDatabase"]
    return LogSecrets(
        {
            service["environment"][1]["secretReference"]: "project-secret-canary",
            database["credentialReference"]: "database-secret-canary",
        }
    )


def test_service_logs_use_exact_container_and_redact_known_secrets() -> None:
    item = runtime_deployment()
    runner = LogRunner(
        project_id=str(item.project_id),
        deployment_id=str(item.id),
        stdout=(
            "2026-08-07T01:00:02.000000000Z project-secret-canary started\n"
            "2026-08-07T01:00:04.000000000Z ready\n"
        ),
        stderr=(
            "2026-08-07T01:00:01.000000000Z database-secret-canary connected\n"
            "2026-08-07T01:00:03.000000000Z warning\n"
        ),
    )

    snapshot = DockerServiceLogReader(runner, _secrets(item)).read(item, None)

    assert snapshot.service_name == "api"
    assert snapshot.services == ("api",)
    assert [line.timestamp for line in snapshot.lines] == sorted(
        line.timestamp for line in snapshot.lines
    )
    assert [line.stream for line in snapshot.lines] == ["STDERR", "STDOUT", "STDERR", "STDOUT"]
    text = "\n".join(line.message for line in snapshot.lines)
    assert text.count("[REDACTED]") == 2
    assert "project-secret-canary" not in text
    assert "database-secret-canary" not in text
    assert runner.calls == [
        [
            "docker",
            "inspect",
            "--format",
            "{{json .Id}} {{json .Config.Labels}}",
            f"hm-p{item.project_id.hex[:12]}-api-g{item.id.hex[:12]}",
        ],
        [
            "docker",
            "logs",
            "--tail",
            "200",
            "--timestamps",
            "f" * 64,
        ],
    ]


def test_service_logs_reject_unknown_service_before_docker_access() -> None:
    item = runtime_deployment()
    runner = LogRunner(project_id=str(item.project_id), deployment_id=str(item.id))

    with pytest.raises(ServiceLogError) as raised:
        DockerServiceLogReader(runner, _secrets(item)).read(item, "unknown")

    assert raised.value.code == "SERVICE_LOG_SERVICE_NOT_FOUND"
    assert runner.calls == []


def test_service_logs_require_exact_project_and_deployment_labels() -> None:
    item = runtime_deployment()
    runner = LogRunner(
        project_id=str(item.project_id),
        deployment_id=str(item.id),
        labels={"heimdall.managed": "true", "heimdall.deployment-id": str(item.id)},
    )

    with pytest.raises(ServiceLogError) as raised:
        DockerServiceLogReader(runner, _secrets(item)).read(item, "api")

    assert raised.value.code == "SERVICE_LOGS_UNAVAILABLE"
    assert all(command[1] != "logs" for command in runner.calls)


def test_service_logs_fail_closed_when_redaction_values_are_unavailable() -> None:
    item = runtime_deployment()
    runner = LogRunner(project_id=str(item.project_id), deployment_id=str(item.id))

    with pytest.raises(ServiceLogError) as raised:
        DockerServiceLogReader(runner, LogSecrets({})).read(item, "api")

    assert raised.value.code == "SERVICE_LOG_REDACTION_UNAVAILABLE"
    assert runner.calls == []


def test_service_logs_fail_closed_for_a_secret_that_cannot_be_redacted_per_line() -> None:
    item = runtime_deployment()
    secrets = _secrets(item)
    reference = item.config_snapshot["services"][0]["environment"][1]["secretReference"]
    secrets.values[reference] = "first-line\nsecond-line"
    runner = LogRunner(project_id=str(item.project_id), deployment_id=str(item.id))

    with pytest.raises(ServiceLogError) as raised:
        DockerServiceLogReader(runner, secrets).read(item, "api")

    assert raised.value.code == "SERVICE_LOG_REDACTION_UNAVAILABLE"
    assert runner.calls == []


def test_service_logs_bound_line_length_and_tail_count() -> None:
    item = runtime_deployment()
    payload = "".join(
        f"2026-08-07T01:00:00.{index:09d}Z line-{index}-{'x' * 20_000}\n"
        for index in range(SERVICE_LOG_TAIL + 5)
    )
    runner = LogRunner(
        project_id=str(item.project_id),
        deployment_id=str(item.id),
        stdout=payload,
    )

    snapshot = DockerServiceLogReader(runner, _secrets(item)).read(item, "api")

    assert len(snapshot.lines) == SERVICE_LOG_TAIL
    assert snapshot.lines[0].message.startswith("line-5-")
    assert max(len(line.message.encode("utf-8")) for line in snapshot.lines) <= (
        SERVICE_LOG_MAX_LINE_BYTES
    )
    assert snapshot.truncated is True


def test_command_runner_captures_stdout_and_stderr_separately() -> None:
    result = SubprocessCommandRunner().run(
        [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr)",
        ],
        timeout_seconds=5,
    )

    assert result.stdout == "out\n"
    assert result.stderr == "err\n"
    assert result.stdout_truncated is False
    assert result.stderr_truncated is False


def test_command_runner_drains_but_bounds_large_process_output() -> None:
    result = SubprocessCommandRunner().run(
        [
            sys.executable,
            "-c",
            (
                "import sys; sys.stdout.write('a' * 299996 + 'TAIL'); "
                "sys.stderr.write('b' * 299996 + 'TAIL')"
            ),
        ],
        timeout_seconds=5,
    )

    assert len(result.stdout.encode("utf-8")) == 262_144
    assert len(result.stderr.encode("utf-8")) == 262_144
    assert result.stdout.endswith("TAIL")
    assert result.stderr.endswith("TAIL")
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True
