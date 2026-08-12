from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from threading import Thread
from typing import Protocol

_MAX_CAPTURE_BYTES = 262_144


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class CommandExecutionError(RuntimeError):
    def __init__(self, result: CommandResult | int) -> None:
        super().__init__("external command failed")
        self.result = result if isinstance(result, CommandResult) else CommandResult(result, "")
        self.returncode = self.result.returncode


class CommandRunner(Protocol):
    def run(
        self,
        arguments: Sequence[str],
        *,
        timeout_seconds: float,
        heartbeat: Callable[[], None] | None = None,
        check: bool = True,
    ) -> CommandResult: ...


class SubprocessCommandRunner:
    def __init__(self, heartbeat_interval_seconds: float = 10) -> None:
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        self._heartbeat_interval_seconds = heartbeat_interval_seconds

    def run(
        self,
        arguments: Sequence[str],
        *,
        timeout_seconds: float,
        heartbeat: Callable[[], None] | None = None,
        check: bool = True,
    ) -> CommandResult:
        if not arguments or any(not isinstance(item, str) or not item for item in arguments):
            raise ValueError("command arguments must be non-empty strings")
        if timeout_seconds <= 0:
            raise ValueError("command timeout must be positive")
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        deadline = time.monotonic() + timeout_seconds
        try:
            process = subprocess.Popen(
                list(arguments),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                start_new_session=True,
            )
        except OSError as error:
            raise CommandExecutionError(-1) from error
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_capture = _BoundedCapture(process.stdout)
        stderr_capture = _BoundedCapture(process.stderr)
        stdout_capture.start()
        stderr_capture.start()
        timeout_error: subprocess.TimeoutExpired | None = None
        returncode = -1
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(arguments, timeout_seconds)
                try:
                    returncode = process.wait(
                        timeout=min(remaining, self._heartbeat_interval_seconds)
                    )
                    break
                except subprocess.TimeoutExpired:
                    if heartbeat is not None:
                        heartbeat()
        except subprocess.TimeoutExpired as error:
            timeout_error = error
            _terminate(process)
            returncode = process.returncode if process.returncode is not None else -1
        except BaseException:
            _terminate(process)
            raise
        finally:
            stdout_capture.join()
            stderr_capture.join()

        result = CommandResult(
            returncode=returncode,
            stdout=stdout_capture.text(),
            stderr=stderr_capture.text(),
            stdout_truncated=stdout_capture.truncated,
            stderr_truncated=stderr_capture.truncated,
        )
        if timeout_error is not None:
            raise CommandExecutionError(result) from timeout_error
        if check and result.returncode != 0:
            raise CommandExecutionError(result)
        return result


def _terminate(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


class _BoundedCapture:
    def __init__(self, stream) -> None:
        self._stream = stream
        self._payload = bytearray()
        self._thread = Thread(target=self._drain, daemon=True)
        self.truncated = False

    def start(self) -> None:
        self._thread.start()

    def join(self) -> None:
        self._thread.join()

    def text(self) -> str:
        return bytes(self._payload).decode("utf-8", errors="replace")

    def _drain(self) -> None:
        with self._stream:
            while chunk := self._stream.read(65_536):
                self._payload.extend(chunk)
                overflow = len(self._payload) - _MAX_CAPTURE_BYTES
                if overflow > 0:
                    del self._payload[:overflow]
                    self.truncated = True
