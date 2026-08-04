from __future__ import annotations

import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str


class CommandExecutionError(RuntimeError):
    def __init__(self, returncode: int) -> None:
        super().__init__("external command failed")
        self.returncode = returncode


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
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            try:
                process = subprocess.Popen(
                    list(arguments),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    env=environment,
                    start_new_session=True,
                )
            except OSError as error:
                raise CommandExecutionError(-1) from error
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
            except BaseException:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise

            stdout_file.seek(0)
            stdout = stdout_file.read(262_144).decode("utf-8", errors="replace")
        result = CommandResult(returncode=returncode, stdout=stdout)
        if check and result.returncode != 0:
            raise CommandExecutionError(result.returncode)
        return result
