from __future__ import annotations

import json
import os
import re
import socket
import stat
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from threading import BoundedSemaphore, Event, Thread
from typing import Any, Protocol
from uuid import UUID

from heimdall.runtime.log_broker import (
    ensure_private_socket_directory,
    remove_exact_owned_socket,
    remove_stale_owned_socket,
)
from heimdall.runtime.logs import (
    SERVICE_LOG_MAX_LINE_BYTES,
    SERVICE_LOG_STREAM_HEARTBEAT_SECONDS,
    SERVICE_LOG_STREAM_MAX_FRAME_BYTES,
    ServiceLogError,
    ServiceLogLine,
    ServiceLogStream,
    ServiceLogStreamEnd,
    ServiceLogStreamEvent,
    ServiceLogStreamLine,
    ServiceLogStreamReady,
)

_PROTOCOL_VERSION = 1
_MAX_REQUEST_BYTES = 8 * 1024
_SERVICE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_PUBLIC_ERROR_CODES = {
    "SERVICE_LOG_SERVICE_NOT_FOUND",
    "SERVICE_LOGS_UNAVAILABLE",
    "SERVICE_LOG_REDACTION_UNAVAILABLE",
    "RUNTIME_LOG_STREAM_BUSY",
}


def service_log_stream_socket_path(runtime_root: Path) -> Path:
    return runtime_root / "log-stream.sock"


class ServiceLogStreamSource(Protocol):
    ready: ServiceLogStreamReady

    def receive(self, timeout_seconds: float) -> ServiceLogStreamEvent | None: ...

    def close(self) -> None: ...


class UnixServiceLogStreamBrokerServer:
    def __init__(
        self,
        path: Path,
        handler: Callable[[UUID, str | None], ServiceLogStreamSource],
        *,
        request_timeout_seconds: float = 2,
        write_timeout_seconds: float = 2,
        heartbeat_seconds: float = SERVICE_LOG_STREAM_HEARTBEAT_SECONDS,
        max_streams: int = 4,
    ) -> None:
        if request_timeout_seconds <= 0 or write_timeout_seconds <= 0 or heartbeat_seconds <= 0:
            raise ValueError("stream broker timeouts must be positive")
        if max_streams < 1:
            raise ValueError("stream broker max streams must be positive")
        self._path = path
        self._handler = handler
        self._request_timeout_seconds = request_timeout_seconds
        self._write_timeout_seconds = write_timeout_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._max_streams = max_streams
        self._listener: socket.socket | None = None
        self._thread: Thread | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._capacity = BoundedSemaphore(max_streams)
        self._stop = Event()
        self._socket_identity: tuple[int, int] | None = None

    def start(self) -> None:
        if self._listener is not None:
            return
        ensure_private_socket_directory(self._path.parent)
        remove_stale_owned_socket(self._path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bound_identity: tuple[int, int] | None = None
        try:
            listener.bind(str(self._path))
            os.chmod(self._path, 0o600, follow_symlinks=False)
            metadata = self._path.lstat()
            bound_identity = (metadata.st_dev, metadata.st_ino)
            if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise OSError("stream broker socket ownership could not be verified")
            listener.listen(self._max_streams)
            listener.settimeout(0.2)
        except BaseException:
            listener.close()
            if bound_identity is not None:
                remove_exact_owned_socket(self._path, bound_identity)
            raise

        self._stop.clear()
        self._listener = listener
        self._socket_identity = bound_identity
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_streams,
            thread_name_prefix="heimdall-service-log-stream",
        )
        self._thread = Thread(
            target=self._serve,
            name="heimdall-service-log-stream-broker",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        listener = self._listener
        self._listener = None
        if listener is not None:
            listener.close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._executor = None
        if self._socket_identity is not None:
            remove_exact_owned_socket(self._path, self._socket_identity)
        self._socket_identity = None

    def _serve(self) -> None:
        listener = self._listener
        executor = self._executor
        if listener is None or executor is None:
            return
        while not self._stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            if not self._capacity.acquire(blocking=False):
                with connection:
                    connection.settimeout(0.1)
                    with suppress(OSError):
                        connection.sendall(_encode_error("RUNTIME_LOG_STREAM_BUSY"))
                continue
            try:
                executor.submit(self._handle_with_capacity, connection)
            except RuntimeError:
                self._capacity.release()
                connection.close()

    def _handle_with_capacity(self, connection: socket.socket) -> None:
        try:
            self._handle(connection)
        finally:
            self._capacity.release()

    def _handle(self, connection: socket.socket) -> None:
        source: ServiceLogStreamSource | None = None
        ready_sent = False
        with connection:
            framed = _FramedSocket(connection)
            connection.settimeout(self._request_timeout_seconds)
            try:
                deployment_id, service_name = _decode_request(framed.receive(_MAX_REQUEST_BYTES))
                source = self._handler(deployment_id, service_name)
                if source.ready.deployment_id != deployment_id:
                    raise ServiceLogError("SERVICE_LOGS_UNAVAILABLE")
                connection.settimeout(self._write_timeout_seconds)
                connection.sendall(_encode_ready(source.ready))
                ready_sent = True
                while not self._stop.is_set():
                    event = source.receive(self._heartbeat_seconds)
                    if event is None:
                        connection.sendall(_encode_heartbeat())
                        continue
                    connection.sendall(_encode_event(event))
                    if isinstance(event, ServiceLogStreamEnd):
                        break
            except ServiceLogError as error:
                payload = (
                    _encode_stream_error(error.code) if ready_sent else _encode_error(error.code)
                )
                with suppress(OSError):
                    connection.sendall(payload)
            except (OSError, TimeoutError, TypeError, ValueError, json.JSONDecodeError):
                if not ready_sent:
                    with suppress(OSError):
                        connection.sendall(_encode_error("RUNTIME_LOG_STREAM_REQUEST_INVALID"))
            except Exception:
                payload = (
                    _encode_stream_error("SERVICE_LOGS_UNAVAILABLE")
                    if ready_sent
                    else _encode_error("SERVICE_LOGS_UNAVAILABLE")
                )
                with suppress(OSError):
                    connection.sendall(payload)
            finally:
                if source is not None:
                    source.close()


class UnixServiceLogStreamBrokerClient:
    def __init__(
        self,
        path: Path,
        *,
        handshake_timeout_seconds: float = 6,
        idle_timeout_seconds: float = SERVICE_LOG_STREAM_HEARTBEAT_SECONDS * 3,
    ) -> None:
        if handshake_timeout_seconds <= 0 or idle_timeout_seconds <= 0:
            raise ValueError("stream broker client timeouts must be positive")
        self._path = path
        self._handshake_timeout_seconds = handshake_timeout_seconds
        self._idle_timeout_seconds = idle_timeout_seconds

    def open(
        self,
        deployment_id: UUID,
        service_name: str | None,
    ) -> UnixServiceLogStreamSubscription:
        request = (
            json.dumps(
                {
                    "version": _PROTOCOL_VERSION,
                    "deploymentId": str(deployment_id),
                    "serviceName": service_name,
                },
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(self._handshake_timeout_seconds)
            connection.connect(str(self._path))
            connection.sendall(request)
            framed = _FramedSocket(connection)
            ready = _decode_ready_or_error(
                framed.receive(SERVICE_LOG_STREAM_MAX_FRAME_BYTES), deployment_id
            )
            connection.settimeout(self._idle_timeout_seconds)
            return UnixServiceLogStreamSubscription(connection, framed, ready)
        except ServiceLogError:
            connection.close()
            raise
        except (OSError, TimeoutError, TypeError, ValueError, json.JSONDecodeError) as error:
            connection.close()
            raise ServiceLogError("RUNTIME_LOG_STREAM_UNAVAILABLE") from error


class UnixServiceLogStreamSubscription:
    def __init__(
        self,
        connection: socket.socket,
        framed: _FramedSocket,
        ready: ServiceLogStreamReady,
    ) -> None:
        self._connection = connection
        self._framed = framed
        self.ready = ready
        self._closed = False

    def receive(self) -> ServiceLogStreamEvent | None:
        if self._closed:
            return ServiceLogStreamEnd()
        try:
            payload = self._framed.receive(SERVICE_LOG_STREAM_MAX_FRAME_BYTES)
            return _decode_event(payload)
        except ServiceLogError:
            raise
        except (OSError, TimeoutError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ServiceLogError("RUNTIME_LOG_STREAM_UNAVAILABLE") from error

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with suppress(OSError):
            self._connection.shutdown(socket.SHUT_RDWR)
        self._connection.close()


class _FramedSocket:
    def __init__(self, connection: socket.socket) -> None:
        self._connection = connection
        self._buffer = bytearray()

    def receive(self, limit: int) -> bytes:
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                if newline > limit:
                    raise ValueError("stream broker frame exceeds limit")
                payload = bytes(self._buffer[:newline])
                del self._buffer[: newline + 1]
                return payload
            if len(self._buffer) > limit:
                raise ValueError("stream broker frame exceeds limit")
            chunk = self._connection.recv(min(8192, limit + 1 - len(self._buffer)))
            if not chunk:
                raise ValueError("stream broker frame ended before newline")
            self._buffer.extend(chunk)


def _decode_request(payload: bytes) -> tuple[UUID, str | None]:
    value = json.loads(payload)
    if not isinstance(value, dict) or set(value) != {
        "version",
        "deploymentId",
        "serviceName",
    }:
        raise ValueError("invalid stream broker request shape")
    if value["version"] != _PROTOCOL_VERSION or not isinstance(value["deploymentId"], str):
        raise ValueError("unsupported stream broker request")
    deployment_id = UUID(value["deploymentId"])
    service_name = value["serviceName"]
    if service_name is not None and (
        not isinstance(service_name, str) or _SERVICE_NAME.fullmatch(service_name) is None
    ):
        raise ValueError("invalid service name")
    return deployment_id, service_name


def _encode_ready(ready: ServiceLogStreamReady) -> bytes:
    return _encode(
        {
            "type": "ready",
            "deploymentId": str(ready.deployment_id),
            "services": list(ready.services),
            "serviceName": ready.service_name,
            "connectedAt": ready.connected_at.isoformat().replace("+00:00", "Z"),
        }
    )


def _encode_event(event: ServiceLogStreamEvent) -> bytes:
    if isinstance(event, ServiceLogStreamLine):
        return _encode(
            {
                "type": "log",
                "timestamp": event.line.timestamp,
                "stream": event.line.stream.value,
                "message": event.line.message,
                "truncated": event.truncated,
            }
        )
    return _encode({"type": "end", "reason": event.reason})


def _encode_heartbeat() -> bytes:
    return _encode({"type": "heartbeat"})


def _encode_error(code: str) -> bytes:
    return _encode({"type": "error", "code": code})


def _encode_stream_error(code: str) -> bytes:
    return _encode({"type": "stream-error", "code": code})


def _encode(value: dict[str, Any]) -> bytes:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > SERVICE_LOG_STREAM_MAX_FRAME_BYTES:
        raise ServiceLogError("SERVICE_LOGS_UNAVAILABLE")
    return payload + b"\n"


def _decode_ready_or_error(payload: bytes, deployment_id: UUID) -> ServiceLogStreamReady:
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("invalid stream broker response")
    if value.get("type") == "error":
        code = value.get("code")
        if code in _PUBLIC_ERROR_CODES:
            raise ServiceLogError(code)
        raise ServiceLogError("RUNTIME_LOG_STREAM_UNAVAILABLE")
    services = value.get("services")
    service_name = value.get("serviceName")
    connected_at = value.get("connectedAt")
    if (
        value.get("type") != "ready"
        or value.get("deploymentId") != str(deployment_id)
        or not isinstance(services, list)
        or not services
        or any(
            not isinstance(item, str) or _SERVICE_NAME.fullmatch(item) is None for item in services
        )
        or len(set(services)) != len(services)
        or not isinstance(service_name, str)
        or service_name not in services
        or not isinstance(connected_at, str)
    ):
        raise ValueError("invalid stream broker ready frame")
    return ServiceLogStreamReady(
        deployment_id=deployment_id,
        services=tuple(services),
        service_name=service_name,
        connected_at=datetime.fromisoformat(connected_at.replace("Z", "+00:00")),
    )


def _decode_event(payload: bytes) -> ServiceLogStreamEvent | None:
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("invalid stream broker event")
    event_type = value.get("type")
    if event_type == "heartbeat" and set(value) == {"type"}:
        return None
    if event_type == "stream-error" and set(value) == {"type", "code"}:
        code = value.get("code")
        if code in _PUBLIC_ERROR_CODES or code == "RUNTIME_LOG_STREAM_UNAVAILABLE":
            raise ServiceLogError(code)
        raise ServiceLogError("RUNTIME_LOG_STREAM_UNAVAILABLE")
    if event_type == "end" and set(value) == {"type", "reason"}:
        reason = value.get("reason")
        if reason != "CONTAINER_LOG_ENDED":
            raise ValueError("invalid stream end reason")
        return ServiceLogStreamEnd(reason)
    if event_type != "log" or set(value) != {
        "type",
        "timestamp",
        "stream",
        "message",
        "truncated",
    }:
        raise ValueError("invalid stream log event")
    timestamp = value.get("timestamp")
    message = value.get("message")
    truncated = value.get("truncated")
    if (
        not isinstance(timestamp, str)
        or not timestamp
        or not isinstance(message, str)
        or len(message.encode("utf-8")) > SERVICE_LOG_MAX_LINE_BYTES
        or not isinstance(truncated, bool)
    ):
        raise ValueError("invalid stream log fields")
    return ServiceLogStreamLine(
        ServiceLogLine(timestamp, ServiceLogStream(value.get("stream")), message),
        truncated,
    )
