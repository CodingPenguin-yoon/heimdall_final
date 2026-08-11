from __future__ import annotations

import json
from collections import deque
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from heimdall.deployments.event_stream import (
    DeploymentEventStreamEnd,
    DeploymentEventStreamError,
    PostgresDeploymentEventStreamGateway,
)
from heimdall.deployments.models import (
    Deployment,
    DeploymentEvent,
    DeploymentSource,
    DeploymentStatus,
)


class Notification:
    def __init__(self, payload: str) -> None:
        self.payload = payload


class ListenerConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.notifications: deque[Notification] = deque()

    def execute(self, statement: str):
        self.statements.append(statement)
        return self

    def commit(self) -> None:
        return None

    def notifies(self, *, timeout: float, stop_after: int):
        assert timeout == 0.01
        assert stop_after == 1
        if self.notifications:
            yield self.notifications.popleft()


class ListenerDatabase:
    def __init__(self) -> None:
        self.connection_value = ListenerConnection()
        self.checked_out = 0

    @contextmanager
    def connection(self):
        self.checked_out += 1
        try:
            yield self.connection_value
        finally:
            self.checked_out -= 1


class EventRepository:
    def __init__(self, deployment: Deployment) -> None:
        self.deployment = deployment
        self.events: list[DeploymentEvent] = []

    def get(self, deployment_id):
        assert deployment_id == self.deployment.id
        return self.deployment

    def list_events_after(self, deployment_id, after_id: int, limit: int = 100):
        assert deployment_id == self.deployment.id
        return [event for event in self.events if event.id > after_id][:limit]


def deployment(status: DeploymentStatus = DeploymentStatus.BUILDING) -> Deployment:
    now = datetime(2026, 8, 10, 1, tzinfo=UTC)
    return Deployment(
        id=uuid4(),
        project_id=uuid4(),
        source_type=DeploymentSource.MAIN_HEAD,
        requested_commit_sha=None,
        resolved_commit_sha="a" * 40,
        config_version=1,
        config_snapshot={},
        status=status,
        failure_stage=None,
        failure_code=None,
        created_at=now,
        updated_at=now,
        terminal_at=(
            now if status in {DeploymentStatus.SUCCEEDED, DeploymentStatus.FAILED} else None
        ),
    )


def event(item: Deployment, event_id: int) -> DeploymentEvent:
    return DeploymentEvent(
        id=event_id,
        deployment_id=item.id,
        stage="BUILDING",
        code=f"EVENT_{event_id}",
        message="building",
        created_at=datetime(2026, 8, 10, 1, tzinfo=UTC),
    )


def test_subscription_replays_cursor_rows_then_uses_notification_as_wakeup() -> None:
    item = deployment()
    repository = EventRepository(item)
    repository.events.append(event(item, 2))
    database = ListenerDatabase()
    gateway = PostgresDeploymentEventStreamGateway(database, repository, heartbeat_seconds=0.01)

    subscription = gateway.open(item.id, 1)

    assert subscription.receive() == repository.events[0]
    repository.events.append(event(item, 3))
    database.connection_value.notifications.append(
        Notification(json.dumps({"deploymentId": str(item.id), "eventId": 3}))
    )
    assert subscription.receive() == repository.events[1]

    subscription.close()
    assert database.checked_out == 0
    assert database.connection_value.statements == [
        "LISTEN heimdall_deployment_events",
        "UNLISTEN heimdall_deployment_events",
    ]


def test_terminal_deployment_ends_after_cursor_is_drained() -> None:
    item = deployment(DeploymentStatus.SUCCEEDED)
    repository = EventRepository(item)
    repository.events.append(event(item, 8))
    database = ListenerDatabase()
    subscription = PostgresDeploymentEventStreamGateway(
        database, repository, heartbeat_seconds=0.01
    ).open(item.id, 7)

    assert subscription.receive() == repository.events[0]
    assert subscription.receive() == DeploymentEventStreamEnd()
    subscription.close()


def test_gateway_rejects_connections_beyond_its_reserved_capacity() -> None:
    item = deployment()
    repository = EventRepository(item)
    database = ListenerDatabase()
    gateway = PostgresDeploymentEventStreamGateway(database, repository, max_subscriptions=1)
    first = gateway.open(item.id, 0)

    with pytest.raises(DeploymentEventStreamError) as raised:
        gateway.open(item.id, 0)

    assert raised.value.code == "DEPLOYMENT_EVENT_STREAM_BUSY"
    first.close()
