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
from typing import Any
from uuid import UUID

from heimdall.runtime.logs import (
    SERVICE_LOG_MAX_LINE_BYTES,
    SERVICE_LOG_MAX_RESPONSE_BYTES,
    SERVICE_LOG_TAIL,
    ServiceLogError,
    ServiceLogLine,
    ServiceLogSnapshot,
    ServiceLogStream,
)

_PROTOCOL_VERSION = 1
_MAX_REQUEST_BYTES = 8 * 1024
_SERVICE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_PUBLIC_ERROR_CODES = {
    "SERVICE_LOG_SERVICE_NOT_FOUND",
    "SERVICE_LOGS_UNAVAILABLE",
    "SERVICE_LOG_REDACTION_UNAVAILABLE",
}


def service_log_socket_path(runtime_root: Path) -> Path:
    return runtime_root / "logs.sock"


class UnixServiceLogBrokerServer:
    def __init__(
        self,
        path: Path,
        handler: Callable[[UUID, str | None], ServiceLogSnapshot],
        *,
        request_timeout_seconds: float = 2,
        max_workers: int = 4,
    ) -> None:
        if request_timeout_seconds <= 0:
            raise ValueError("broker request timeout must be positive")
        if max_workers < 1:
            raise ValueError("broker max workers must be positive")
        self._path = path
        self._handler = handler
        self._request_timeout_seconds = request_timeout_seconds
        self._max_workers = max_workers
        self._listener: socket.socket | None = None
        self._thread: Thread | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._capacity = BoundedSemaphore(max_workers)
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
                raise OSError("broker socket ownership could not be verified")
            listener.listen(self._max_workers)
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
            max_workers=self._max_workers,
            thread_name_prefix="heimdall-service-logs",
        )
        self._thread = Thread(
            target=self._serve,
            name="heimdall-service-log-broker",
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
                        connection.sendall(_encode_error("RUNTIME_LOG_BROKER_BUSY"))
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
        with connection:
            connection.settimeout(self._request_timeout_seconds)
            try:
                deployment_id, service_name = _decode_request(
                    _receive_line(connection, _MAX_REQUEST_BYTES)
                )
                snapshot = self._handler(deployment_id, service_name)
                if snapshot.deployment_id != deployment_id:
                    raise ServiceLogError("SERVICE_LOGS_UNAVAILABLE")
                response = _encode_snapshot(snapshot)
            except ServiceLogError as error:
                response = _encode_error(error.code)
            except (OSError, TimeoutError, TypeError, ValueError, json.JSONDecodeError):
                response = _encode_error("RUNTIME_LOG_BROKER_REQUEST_INVALID")
            except Exception:
                response = _encode_error("SERVICE_LOGS_UNAVAILABLE")
            try:
                connection.sendall(response)
            except OSError:
                return


class UnixServiceLogBrokerClient:
    def __init__(self, path: Path, *, timeout_seconds: float = 3) -> None:
        if timeout_seconds <= 0:
            raise ValueError("broker client timeout must be positive")
        self._path = path
        self._timeout_seconds = timeout_seconds

    def fetch(self, deployment_id: UUID, service_name: str | None) -> ServiceLogSnapshot:
        request = (
            json.dumps(
                {
                    "version": _PROTOCOL_VERSION,
                    "deploymentId": str(deployment_id),
                    "serviceName": service_name,
                    "tail": SERVICE_LOG_TAIL,
                },
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self._timeout_seconds)
                connection.connect(str(self._path))
                connection.sendall(request)
                connection.shutdown(socket.SHUT_WR)
                response = _receive_line(connection, SERVICE_LOG_MAX_RESPONSE_BYTES)
            return _decode_snapshot(response, deployment_id)
        except ServiceLogError:
            raise
        except (OSError, TimeoutError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ServiceLogError("RUNTIME_LOG_BROKER_UNAVAILABLE") from error


def _decode_request(payload: bytes) -> tuple[UUID, str | None]:
    value = json.loads(payload)
    if not isinstance(value, dict) or set(value) != {
        "version",
        "deploymentId",
        "serviceName",
        "tail",
    }:
        raise ValueError("invalid broker request shape")
    if value["version"] != _PROTOCOL_VERSION or value["tail"] != SERVICE_LOG_TAIL:
        raise ValueError("unsupported broker request")
    if not isinstance(value["deploymentId"], str):
        raise ValueError("invalid deployment id")
    deployment_id = UUID(value["deploymentId"])
    service_name = value["serviceName"]
    if service_name is not None and (
        not isinstance(service_name, str) or _SERVICE_NAME.fullmatch(service_name) is None
    ):
        raise ValueError("invalid service name")
    return deployment_id, service_name


def _encode_snapshot(snapshot: ServiceLogSnapshot) -> bytes:
    lines = [
        {
            "timestamp": line.timestamp,
            "stream": line.stream.value,
            "message": line.message,
        }
        for line in snapshot.lines
    ]
    truncated = snapshot.truncated
    while True:
        payload = {
            "ok": True,
            "deploymentId": str(snapshot.deployment_id),
            "services": list(snapshot.services),
            "serviceName": snapshot.service_name,
            "retrievedAt": snapshot.retrieved_at.isoformat().replace("+00:00", "Z"),
            "lines": lines,
            "truncated": truncated,
        }
        encoded = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        )
        if len(encoded) <= SERVICE_LOG_MAX_RESPONSE_BYTES:
            return encoded
        if not lines:
            raise ServiceLogError("SERVICE_LOGS_UNAVAILABLE")
        lines.pop(0)
        truncated = True


def _encode_error(code: str) -> bytes:
    return (
        json.dumps({"ok": False, "error": {"code": code}}, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )


def _decode_snapshot(payload: bytes, deployment_id: UUID) -> ServiceLogSnapshot:
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("invalid broker response")
    if value.get("ok") is False:
        error = value.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        if code in _PUBLIC_ERROR_CODES:
            raise ServiceLogError(code)
        raise ServiceLogError("RUNTIME_LOG_BROKER_UNAVAILABLE")
    if value.get("ok") is not True or value.get("deploymentId") != str(deployment_id):
        raise ValueError("broker deployment mismatch")

    raw_services = value.get("services")
    service_name = value.get("serviceName")
    raw_lines = value.get("lines")
    truncated = value.get("truncated")
    retrieved_at = value.get("retrievedAt")
    if (
        not isinstance(raw_services, list)
        or not raw_services
        or any(
            not isinstance(item, str) or _SERVICE_NAME.fullmatch(item) is None
            for item in raw_services
        )
        or len(set(raw_services)) != len(raw_services)
        or not isinstance(service_name, str)
        or service_name not in raw_services
        or not isinstance(raw_lines, list)
        or len(raw_lines) > SERVICE_LOG_TAIL
        or not isinstance(truncated, bool)
        or not isinstance(retrieved_at, str)
    ):
        raise ValueError("invalid broker response fields")

    lines = tuple(_decode_line(item) for item in raw_lines)
    return ServiceLogSnapshot(
        deployment_id=deployment_id,
        services=tuple(raw_services),
        service_name=service_name,
        retrieved_at=datetime.fromisoformat(retrieved_at.replace("Z", "+00:00")),
        lines=lines,
        truncated=truncated,
    )


def _decode_line(value: Any) -> ServiceLogLine:
    if not isinstance(value, dict) or set(value) != {"timestamp", "stream", "message"}:
        raise ValueError("invalid service log line")
    timestamp = value["timestamp"]
    message = value["message"]
    if (
        not isinstance(timestamp, str)
        or not timestamp
        or not isinstance(message, str)
        or len(message.encode("utf-8")) > SERVICE_LOG_MAX_LINE_BYTES
    ):
        raise ValueError("invalid service log line fields")
    return ServiceLogLine(
        timestamp=timestamp,
        stream=ServiceLogStream(value["stream"]),
        message=message,
    )


def _receive_line(connection: socket.socket, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = connection.recv(min(8192, limit + 1 - size))
        if not chunk:
            raise ValueError("broker message ended before newline")
        chunks.append(chunk)
        size += len(chunk)
        if size > limit:
            raise ValueError("broker message exceeds limit")
        if b"\n" in chunk:
            payload = b"".join(chunks)
            line, separator, trailing = payload.partition(b"\n")
            if not separator or trailing:
                raise ValueError("broker message framing is invalid")
            return line


def ensure_private_socket_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise OSError("broker directory is unsafe")
    os.chmod(path, 0o700, follow_symlinks=False)


def remove_stale_owned_socket(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise OSError("broker path is not an owned socket")

    active = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    active.settimeout(0.1)
    try:
        active.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError):
        pass
    except OSError as error:
        raise OSError("broker socket state is uncertain") from error
    else:
        raise OSError("another broker is already active")
    finally:
        active.close()

    remove_exact_owned_socket(path, (metadata.st_dev, metadata.st_ino))


def remove_exact_owned_socket(path: Path, identity: tuple[int, int]) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.geteuid():
        return
    if (metadata.st_dev, metadata.st_ino) != identity:
        return
    path.unlink()
