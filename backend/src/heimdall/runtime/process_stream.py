from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from queue import Empty, Full, Queue
from threading import Event, Thread
from typing import BinaryIO, Protocol

from heimdall.runtime.process import CommandExecutionError

_STREAM_QUEUE_CAPACITY = 128
_READER_CHUNK_BYTES = 65_536


class CommandOutputStream(StrEnum):
    STDOUT = "STDOUT"
    STDERR = "STDERR"


@dataclass(frozen=True, slots=True)
class CommandOutputLine:
    stream: CommandOutputStream
    payload: bytes
    truncated: bool


@dataclass(frozen=True, slots=True)
class CommandStreamEnded(RuntimeError):
    returncode: int


class CommandLineStream(Protocol):
    def receive(self, timeout_seconds: float) -> CommandOutputLine | None: ...

    def close(self) -> None: ...


class CommandStreamRunner(Protocol):
    def open(self, arguments: Sequence[str], *, max_line_bytes: int) -> CommandLineStream: ...


@dataclass(frozen=True, slots=True)
class _ReaderDone:
    stream: CommandOutputStream


class SubprocessCommandStreamRunner:
    def open(self, arguments: Sequence[str], *, max_line_bytes: int) -> CommandLineStream:
        if not arguments or any(not isinstance(item, str) or not item for item in arguments):
            raise ValueError("command arguments must be non-empty strings")
        if max_line_bytes < 1:
            raise ValueError("stream line limit must be positive")
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
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
        return _SubprocessCommandLineStream(process, max_line_bytes=max_line_bytes)


class _SubprocessCommandLineStream:
    def __init__(self, process: subprocess.Popen[bytes], *, max_line_bytes: int) -> None:
        assert process.stdout is not None
        assert process.stderr is not None
        self._process = process
        self._max_line_bytes = max_line_bytes
        self._queue: Queue[CommandOutputLine | _ReaderDone] = Queue(maxsize=_STREAM_QUEUE_CAPACITY)
        self._stop = Event()
        self._closed = False
        self._done_streams: set[CommandOutputStream] = set()
        self._threads = (
            Thread(
                target=self._read,
                args=(process.stdout, CommandOutputStream.STDOUT),
                name=f"command-stream-stdout-{process.pid}",
                daemon=True,
            ),
            Thread(
                target=self._read,
                args=(process.stderr, CommandOutputStream.STDERR),
                name=f"command-stream-stderr-{process.pid}",
                daemon=True,
            ),
        )
        for thread in self._threads:
            thread.start()

    def receive(self, timeout_seconds: float) -> CommandOutputLine | None:
        if timeout_seconds <= 0:
            raise ValueError("stream receive timeout must be positive")
        if self._closed:
            raise CommandStreamEnded(self._process.poll() or 0)
        while True:
            if len(self._done_streams) == 2 and self._queue.empty():
                raise CommandStreamEnded(self._wait_returncode())
            try:
                item = self._queue.get(timeout=timeout_seconds)
            except Empty:
                if len(self._done_streams) == 2:
                    raise CommandStreamEnded(self._wait_returncode()) from None
                return None
            if isinstance(item, _ReaderDone):
                self._done_streams.add(item.stream)
                continue
            return item

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        self._terminate_process()
        for thread in self._threads:
            thread.join(timeout=1)

    def _read(self, stream: BinaryIO, output_stream: CommandOutputStream) -> None:
        try:
            with stream:
                while not self._stop.is_set():
                    payload = stream.readline(self._max_line_bytes + 1)
                    if not payload:
                        break
                    truncated = len(payload) > self._max_line_bytes
                    retained = payload[: self._max_line_bytes]
                    if not payload.endswith(b"\n"):
                        truncated = True
                        while not self._stop.is_set():
                            discarded = stream.readline(_READER_CHUNK_BYTES)
                            if not discarded or discarded.endswith(b"\n"):
                                break
                    if not self._offer(
                        CommandOutputLine(
                            stream=output_stream,
                            payload=retained.rstrip(b"\r\n"),
                            truncated=truncated,
                        )
                    ):
                        return
        finally:
            self._offer(_ReaderDone(output_stream))

    def _offer(self, item: CommandOutputLine | _ReaderDone) -> bool:
        while not self._stop.is_set():
            try:
                self._queue.put(item, timeout=0.1)
                return True
            except Full:
                continue
        return False

    def _wait_returncode(self) -> int:
        try:
            return self._process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self._terminate_process()
            return self._process.wait()

    def _terminate_process(self) -> None:
        if self._process.poll() is not None:
            return
        try:
            os.killpg(self._process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            self._process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(self._process.pid, signal.SIGKILL)
            self._process.wait()
