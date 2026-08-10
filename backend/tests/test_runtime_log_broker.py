from __future__ import annotations

import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from uuid import UUID, uuid4

import pytest

from heimdall.runtime.log_broker import (
    UnixServiceLogBrokerClient,
    UnixServiceLogBrokerServer,
    service_log_socket_path,
)
from heimdall.runtime.logs import (
    SERVICE_LOG_MAX_LINE_BYTES,
    SERVICE_LOG_TAIL,
    ServiceLogError,
    ServiceLogLine,
    ServiceLogSnapshot,
    ServiceLogStream,
)


@pytest.fixture
def socket_root():
    with tempfile.TemporaryDirectory(prefix="hm-log-broker-", dir="/tmp") as root:
        yield Path(root)


def _snapshot(deployment_id: UUID, *, lines: tuple[ServiceLogLine, ...] = ()):
    return ServiceLogSnapshot(
        deployment_id=deployment_id,
        services=("web", "api"),
        service_name="web",
        retrieved_at=datetime(2026, 8, 7, 1, tzinfo=UTC),
        lines=lines,
        truncated=False,
    )


def test_broker_round_trip_uses_owner_only_socket_and_removes_it_on_stop(
    socket_root: Path,
) -> None:
    deployment_id = uuid4()
    requests: list[tuple[UUID, str | None]] = []
    path = service_log_socket_path(socket_root)

    def handle(requested_id: UUID, service_name: str | None) -> ServiceLogSnapshot:
        requests.append((requested_id, service_name))
        return _snapshot(
            requested_id,
            lines=(
                ServiceLogLine(
                    "2026-08-07T01:00:00.000000000Z",
                    ServiceLogStream.STDOUT,
                    "ready",
                ),
            ),
        )

    server = UnixServiceLogBrokerServer(path, handle)
    server.start()
    try:
        snapshot = UnixServiceLogBrokerClient(path).fetch(deployment_id, None)

        assert requests == [(deployment_id, None)]
        assert snapshot.lines[0].message == "ready"
        assert snapshot.retrieved_at == datetime(2026, 8, 7, 1, tzinfo=UTC)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    finally:
        server.stop()

    assert not path.exists()


def test_broker_bounds_encoded_response_and_preserves_the_newest_lines(
    socket_root: Path,
) -> None:
    deployment_id = uuid4()
    path = service_log_socket_path(socket_root)
    lines = tuple(
        ServiceLogLine(
            f"2026-08-07T01:00:00.{index:09d}Z",
            ServiceLogStream.STDOUT,
            f"line-{index}-" + "x" * (SERVICE_LOG_MAX_LINE_BYTES - 20),
        )
        for index in range(SERVICE_LOG_TAIL)
    )
    server = UnixServiceLogBrokerServer(
        path, lambda requested_id, _: _snapshot(requested_id, lines=lines)
    )
    server.start()
    try:
        snapshot = UnixServiceLogBrokerClient(path).fetch(deployment_id, "web")
    finally:
        server.stop()

    assert snapshot.truncated is True
    assert 0 < len(snapshot.lines) < SERVICE_LOG_TAIL
    assert snapshot.lines[-1].message.startswith(f"line-{SERVICE_LOG_TAIL - 1}-")


def test_broker_does_not_replace_a_regular_file(socket_root: Path) -> None:
    path = service_log_socket_path(socket_root)
    path.write_text("owned by another component")
    server = UnixServiceLogBrokerServer(path, lambda requested_id, _: _snapshot(requested_id))

    with pytest.raises(OSError):
        server.start()

    assert path.read_text() == "owned by another component"


def test_second_broker_does_not_remove_an_active_socket(socket_root: Path) -> None:
    deployment_id = uuid4()
    path = service_log_socket_path(socket_root)
    first = UnixServiceLogBrokerServer(path, lambda requested_id, _: _snapshot(requested_id))
    second = UnixServiceLogBrokerServer(path, lambda requested_id, _: _snapshot(requested_id))
    first.start()
    try:
        with pytest.raises(OSError):
            second.start()
        second.stop()
        assert UnixServiceLogBrokerClient(path).fetch(deployment_id, None).deployment_id == (
            deployment_id
        )
    finally:
        first.stop()


def test_client_maps_missing_worker_socket_to_stable_unavailable_error(
    socket_root: Path,
) -> None:
    with pytest.raises(ServiceLogError) as raised:
        UnixServiceLogBrokerClient(socket_root / "missing.sock", timeout_seconds=0.1).fetch(
            uuid4(), None
        )

    assert raised.value.code == "RUNTIME_LOG_BROKER_UNAVAILABLE"


def test_broker_preserves_public_fail_closed_error(socket_root: Path) -> None:
    path = service_log_socket_path(socket_root)

    def fail(_: UUID, __: str | None) -> ServiceLogSnapshot:
        raise ServiceLogError("SERVICE_LOG_REDACTION_UNAVAILABLE")

    server = UnixServiceLogBrokerServer(path, fail)
    server.start()
    try:
        with pytest.raises(ServiceLogError) as raised:
            UnixServiceLogBrokerClient(path).fetch(uuid4(), "web")
    finally:
        server.stop()

    assert raised.value.code == "SERVICE_LOG_REDACTION_UNAVAILABLE"


def test_broker_rejects_work_above_the_concurrency_limit(socket_root: Path) -> None:
    path = service_log_socket_path(socket_root)
    started = Event()
    release = Event()
    first_result: list[ServiceLogSnapshot] = []

    def block(requested_id: UUID, _: str | None) -> ServiceLogSnapshot:
        started.set()
        assert release.wait(timeout=3)
        return _snapshot(requested_id)

    server = UnixServiceLogBrokerServer(path, block, max_workers=1)
    server.start()
    first = Thread(
        target=lambda: first_result.append(UnixServiceLogBrokerClient(path).fetch(uuid4(), None))
    )
    first.start()
    try:
        assert started.wait(timeout=1)
        with pytest.raises(ServiceLogError) as raised:
            UnixServiceLogBrokerClient(path).fetch(uuid4(), None)
        assert raised.value.code == "RUNTIME_LOG_BROKER_UNAVAILABLE"
    finally:
        release.set()
        first.join(timeout=3)
        server.stop()

    assert len(first_result) == 1


def test_client_times_out_with_stable_unavailable_error(socket_root: Path) -> None:
    path = service_log_socket_path(socket_root)
    release = Event()

    def block(requested_id: UUID, _: str | None) -> ServiceLogSnapshot:
        assert release.wait(timeout=1)
        return _snapshot(requested_id)

    server = UnixServiceLogBrokerServer(path, block)
    server.start()
    try:
        with pytest.raises(ServiceLogError) as raised:
            UnixServiceLogBrokerClient(path, timeout_seconds=0.05).fetch(uuid4(), None)
        assert raised.value.code == "RUNTIME_LOG_BROKER_UNAVAILABLE"
    finally:
        release.set()
        server.stop()
