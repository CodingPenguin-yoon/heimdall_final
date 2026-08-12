from __future__ import annotations

import json
import sys

import pytest
from test_runtime_models import runtime_deployment

from heimdall.runtime.docker_logs import DockerServiceLogReader, DockerServiceLogStreamer
from heimdall.runtime.logs import (
    SERVICE_LOG_MAX_LINE_BYTES,
    SERVICE_LOG_TAIL,
    ServiceLogError,
    ServiceLogStreamEnd,
    ServiceLogStreamLine,
)
from heimdall.runtime.process import (
    CommandExecutionError,
    CommandResult,
    SubprocessCommandRunner,
)
from heimdall.runtime.process_stream import (
    CommandOutputLine,
    CommandOutputStream,
    CommandStreamEnded,
)
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


class LogLineStream:
    def __init__(self, items, *, returncode: int = 0) -> None:
        self.items = list(items)
        self.returncode = returncode
        self.closed = False

    def receive(self, timeout_seconds: float):
        assert timeout_seconds > 0
        if self.items:
            return self.items.pop(0)
        raise CommandStreamEnded(self.returncode)

    def close(self) -> None:
        self.closed = True


class LogStreamRunner:
    def __init__(self, stream: LogLineStream) -> None:
        self.stream = stream
        self.calls = []

    def open(self, arguments, *, max_line_bytes: int):
        self.calls.append((list(arguments), max_line_bytes))
        return self.stream


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


def test_service_log_stream_follows_immutable_container_and_redacts_each_line() -> None:
    item = runtime_deployment()
    inspect_runner = LogRunner(
        project_id=str(item.project_id),
        deployment_id=str(item.id),
    )
    command_stream = LogLineStream(
        [
            CommandOutputLine(
                CommandOutputStream.STDOUT,
                b"2026-08-07T01:00:00.000000000Z project-secret-canary ready",
                False,
            ),
            CommandOutputLine(
                CommandOutputStream.STDERR,
                b"2026-08-07T01:00:01.000000000Z database-secret-canary warning",
                False,
            ),
        ]
    )
    stream_runner = LogStreamRunner(command_stream)

    subscription = DockerServiceLogStreamer(
        inspect_runner,
        stream_runner,
        _secrets(item),
    ).open(item, "api")

    first = subscription.receive(1)
    second = subscription.receive(1)
    ended = subscription.receive(1)
    subscription.close()

    assert subscription.ready.service_name == "api"
    assert isinstance(first, ServiceLogStreamLine)
    assert isinstance(second, ServiceLogStreamLine)
    assert first.line.message == "[REDACTED] ready"
    assert second.line.message == "[REDACTED] warning"
    assert first.line.stream == "STDOUT"
    assert second.line.stream == "STDERR"
    assert isinstance(ended, ServiceLogStreamEnd)
    assert command_stream.closed is True
    assert stream_runner.calls == [
        (
            [
                "docker",
                "logs",
                "--tail",
                "200",
                "--follow",
                "--timestamps",
                "f" * 64,
            ],
            SERVICE_LOG_MAX_LINE_BYTES + len(b"database-secret-canary") + 128,
        )
    ]


def test_service_log_stream_bounds_redacted_output_and_marks_truncation() -> None:
    item = runtime_deployment()
    inspect_runner = LogRunner(
        project_id=str(item.project_id),
        deployment_id=str(item.id),
    )
    command_stream = LogLineStream(
        [
            CommandOutputLine(
                CommandOutputStream.STDOUT,
                b"2026-08-07T01:00:00.000000000Z " + b"x" * (SERVICE_LOG_MAX_LINE_BYTES + 10),
                True,
            )
        ]
    )
    subscription = DockerServiceLogStreamer(
        inspect_runner,
        LogStreamRunner(command_stream),
        _secrets(item),
    ).open(item, "api")

    event = subscription.receive(1)
    subscription.close()

    assert isinstance(event, ServiceLogStreamLine)
    assert len(event.line.message.encode("utf-8")) == SERVICE_LOG_MAX_LINE_BYTES
    assert event.truncated is True


def test_service_log_stream_keeps_invalid_utf8_replacement_within_byte_limit() -> None:
    item = runtime_deployment()
    inspect_runner = LogRunner(
        project_id=str(item.project_id),
        deployment_id=str(item.id),
    )
    command_stream = LogLineStream(
        [
            CommandOutputLine(
                CommandOutputStream.STDOUT,
                b"2026-08-07T01:00:00.000000000Z " + b"\xff" * SERVICE_LOG_MAX_LINE_BYTES,
                False,
            )
        ]
    )
    subscription = DockerServiceLogStreamer(
        inspect_runner,
        LogStreamRunner(command_stream),
        _secrets(item),
    ).open(item, "api")

    event = subscription.receive(1)
    subscription.close()

    assert isinstance(event, ServiceLogStreamLine)
    assert len(event.line.message.encode("utf-8")) <= SERVICE_LOG_MAX_LINE_BYTES
    assert event.truncated is True


def test_service_log_stream_maps_nonzero_follow_exit_to_stable_error() -> None:
    item = runtime_deployment()
    inspect_runner = LogRunner(
        project_id=str(item.project_id),
        deployment_id=str(item.id),
    )
    subscription = DockerServiceLogStreamer(
        inspect_runner,
        LogStreamRunner(LogLineStream([], returncode=1)),
        _secrets(item),
    ).open(item, "api")

    with pytest.raises(ServiceLogError) as raised:
        subscription.receive(1)
    subscription.close()

    assert raised.value.code == "SERVICE_LOGS_UNAVAILABLE"


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


def test_command_runner_preserves_bounded_output_on_failure() -> None:
    with pytest.raises(CommandExecutionError) as raised:
        SubprocessCommandRunner().run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; print('before failure'); "
                    "print('reason', file=sys.stderr); sys.exit(17)"
                ),
            ],
            timeout_seconds=5,
        )

    assert raised.value.returncode == 17
    assert raised.value.result.stdout == "before failure\n"
    assert raised.value.result.stderr == "reason\n"


def test_command_runner_preserves_partial_output_on_timeout() -> None:
    with pytest.raises(CommandExecutionError) as raised:
        SubprocessCommandRunner(heartbeat_interval_seconds=0.05).run(
            [
                sys.executable,
                "-c",
                "import sys, time; print('before timeout', flush=True); time.sleep(5)",
            ],
            timeout_seconds=0.1,
        )

    assert raised.value.result.stdout == "before timeout\n"
