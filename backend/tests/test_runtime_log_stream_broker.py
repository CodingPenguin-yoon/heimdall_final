from __future__ import annotations

import stat
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest

from heimdall.runtime.log_stream_broker import (
    UnixServiceLogStreamBrokerClient,
    UnixServiceLogStreamBrokerServer,
    service_log_stream_socket_path,
)
from heimdall.runtime.logs import (
    ServiceLogError,
    ServiceLogLine,
    ServiceLogStream,
    ServiceLogStreamEnd,
    ServiceLogStreamLine,
    ServiceLogStreamReady,
)


@pytest.fixture
def socket_root():
    with tempfile.TemporaryDirectory(prefix="hm-log-stream-", dir="/tmp") as root:
        yield Path(root)


class Source:
    def __init__(self, deployment_id: UUID, events=()) -> None:
        self.ready = ServiceLogStreamReady(
            deployment_id,
            ("web", "api"),
            "web",
            datetime(2026, 8, 10, 1, tzinfo=UTC),
        )
        self.events = list(events)
        self.closed = Event()

    def receive(self, timeout_seconds: float):
        assert timeout_seconds > 0
        if self.events:
            return self.events.pop(0)
        return ServiceLogStreamEnd()

    def close(self) -> None:
        self.closed.set()


class IdleSource(Source):
    def receive(self, timeout_seconds: float):
        self.closed.wait(timeout_seconds)
        return None


def test_stream_broker_round_trip_is_bounded_and_owner_only(socket_root: Path) -> None:
    deployment_id = uuid4()
    path = service_log_stream_socket_path(socket_root)
    source = Source(
        deployment_id,
        events=(
            None,
            ServiceLogStreamLine(
                ServiceLogLine(
                    "2026-08-10T01:00:00.000000000Z",
                    ServiceLogStream.STDOUT,
                    "ready",
                ),
                False,
            ),
            ServiceLogStreamEnd(),
        ),
    )
    server = UnixServiceLogStreamBrokerServer(path, lambda *_: source)
    server.start()
    try:
        subscription = UnixServiceLogStreamBrokerClient(path).open(deployment_id, None)
        assert subscription.ready.service_name == "web"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

        assert subscription.receive() is None
        line = subscription.receive()
        assert isinstance(line, ServiceLogStreamLine)
        assert line.line.message == "ready"
        assert isinstance(subscription.receive(), ServiceLogStreamEnd)
        subscription.close()
    finally:
        server.stop()

    assert source.closed.is_set()
    assert not path.exists()


def test_stream_broker_client_disconnect_closes_worker_source(socket_root: Path) -> None:
    deployment_id = uuid4()
    path = service_log_stream_socket_path(socket_root)
    source = IdleSource(deployment_id)
    server = UnixServiceLogStreamBrokerServer(
        path,
        lambda *_: source,
        heartbeat_seconds=0.02,
    )
    server.start()
    try:
        subscription = UnixServiceLogStreamBrokerClient(path).open(deployment_id, "web")
        subscription.close()
        assert source.closed.wait(timeout=1)
    finally:
        server.stop()


def test_stream_broker_rejects_above_live_capacity_without_blocking_snapshot_socket(
    socket_root: Path,
) -> None:
    first_id = uuid4()
    path = service_log_stream_socket_path(socket_root)
    source = IdleSource(first_id)
    server = UnixServiceLogStreamBrokerServer(
        path,
        lambda *_: source,
        heartbeat_seconds=0.05,
        max_streams=1,
    )
    server.start()
    first = UnixServiceLogStreamBrokerClient(path).open(first_id, None)
    try:
        with pytest.raises(ServiceLogError) as raised:
            UnixServiceLogStreamBrokerClient(path).open(uuid4(), None)
        assert raised.value.code == "RUNTIME_LOG_STREAM_BUSY"
    finally:
        first.close()
        server.stop()


def test_stream_broker_preserves_fail_closed_error_before_ready(socket_root: Path) -> None:
    path = service_log_stream_socket_path(socket_root)

    def fail(*_):
        raise ServiceLogError("SERVICE_LOG_REDACTION_UNAVAILABLE")

    server = UnixServiceLogStreamBrokerServer(path, fail)
    server.start()
    try:
        with pytest.raises(ServiceLogError) as raised:
            UnixServiceLogStreamBrokerClient(path).open(uuid4(), "web")
        assert raised.value.code == "SERVICE_LOG_REDACTION_UNAVAILABLE"
    finally:
        server.stop()


def test_stream_broker_missing_socket_maps_to_stable_unavailable(socket_root: Path) -> None:
    started = time.monotonic()
    with pytest.raises(ServiceLogError) as raised:
        UnixServiceLogStreamBrokerClient(
            socket_root / "missing.sock", handshake_timeout_seconds=0.1
        ).open(uuid4(), None)

    assert raised.value.code == "RUNTIME_LOG_STREAM_UNAVAILABLE"
    assert time.monotonic() - started < 1
