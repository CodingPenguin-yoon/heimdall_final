from __future__ import annotations

import json
import sys
from collections import deque
from dataclasses import dataclass
from threading import BoundedSemaphore, Lock
from typing import Any
from uuid import UUID

from heimdall.database import Database
from heimdall.deployments.models import DeploymentEvent, DeploymentStatus
from heimdall.deployments.repository import (
    DEPLOYMENT_EVENT_CHANNEL,
    DeploymentRepository,
)


class DeploymentEventStreamError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class DeploymentEventStreamReady:
    deployment_id: UUID
    after_id: int


@dataclass(frozen=True, slots=True)
class DeploymentEventStreamEnd:
    reason: str = "DEPLOYMENT_TERMINAL"


class PostgresDeploymentEventStreamGateway:
    def __init__(
        self,
        database: Database,
        repository: DeploymentRepository,
        *,
        max_subscriptions: int = 4,
        heartbeat_seconds: float = 5.0,
    ) -> None:
        self._database = database
        self._repository = repository
        self._capacity = BoundedSemaphore(max_subscriptions)
        self._heartbeat_seconds = heartbeat_seconds

    def open(self, deployment_id: UUID, after_id: int) -> PostgresDeploymentEventSubscription:
        if not self._capacity.acquire(blocking=False):
            raise DeploymentEventStreamError("DEPLOYMENT_EVENT_STREAM_BUSY")

        connection_context = self._database.connection()
        try:
            connection = connection_context.__enter__()
        except Exception as error:
            self._capacity.release()
            raise DeploymentEventStreamError("DEPLOYMENT_EVENT_STREAM_UNAVAILABLE") from error
        try:
            connection.execute(f"LISTEN {DEPLOYMENT_EVENT_CHANNEL}")
            connection.commit()
        except Exception as error:
            connection_context.__exit__(*sys.exc_info())
            self._capacity.release()
            raise DeploymentEventStreamError("DEPLOYMENT_EVENT_STREAM_UNAVAILABLE") from error

        return PostgresDeploymentEventSubscription(
            deployment_id=deployment_id,
            after_id=after_id,
            repository=self._repository,
            connection_context=connection_context,
            connection=connection,
            capacity=self._capacity,
            heartbeat_seconds=self._heartbeat_seconds,
        )


class PostgresDeploymentEventSubscription:
    def __init__(
        self,
        *,
        deployment_id: UUID,
        after_id: int,
        repository: DeploymentRepository,
        connection_context: Any,
        connection: Any,
        capacity: BoundedSemaphore,
        heartbeat_seconds: float,
    ) -> None:
        self.ready = DeploymentEventStreamReady(deployment_id, after_id)
        self._deployment_id = deployment_id
        self._cursor = after_id
        self._repository = repository
        self._connection_context = connection_context
        self._connection = connection
        self._capacity = capacity
        self._heartbeat_seconds = heartbeat_seconds
        self._buffer: deque[DeploymentEvent] = deque()
        self._lock = Lock()
        self._closed = False

    def receive(self) -> DeploymentEvent | DeploymentEventStreamEnd | None:
        with self._lock:
            if self._closed:
                return DeploymentEventStreamEnd("STREAM_CLOSED")
            try:
                event = self._next_event()
                if event is not None:
                    return event
                if self._terminal():
                    return DeploymentEventStreamEnd()

                for notification in self._connection.notifies(
                    timeout=self._heartbeat_seconds,
                    stop_after=1,
                ):
                    if self._matches(notification.payload):
                        break

                event = self._next_event()
                if event is not None:
                    return event
                if self._terminal():
                    return DeploymentEventStreamEnd()
                return None
            except DeploymentEventStreamError:
                raise
            except Exception as error:
                raise DeploymentEventStreamError("DEPLOYMENT_EVENT_STREAM_UNAVAILABLE") from error

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._connection.execute(f"UNLISTEN {DEPLOYMENT_EVENT_CHANNEL}")
                self._connection.commit()
            finally:
                try:
                    self._connection_context.__exit__(None, None, None)
                finally:
                    self._capacity.release()

    def _next_event(self) -> DeploymentEvent | None:
        if not self._buffer:
            self._buffer.extend(
                self._repository.list_events_after(self._deployment_id, self._cursor, limit=100)
            )
        if not self._buffer:
            return None
        event = self._buffer.popleft()
        self._cursor = event.id
        return event

    def _terminal(self) -> bool:
        deployment = self._repository.get(self._deployment_id)
        return deployment.status in {DeploymentStatus.SUCCEEDED, DeploymentStatus.FAILED}

    def _matches(self, payload: str) -> bool:
        try:
            value = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            return False
        return value.get("deploymentId") == str(self._deployment_id)
