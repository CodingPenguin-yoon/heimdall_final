from __future__ import annotations

import os
import sys
import time

import pytest

from heimdall.runtime.process_stream import (
    CommandOutputStream,
    CommandStreamEnded,
    SubprocessCommandStreamRunner,
)


def test_command_stream_separates_stdout_and_stderr_and_reports_exit() -> None:
    stream = SubprocessCommandStreamRunner().open(
        [
            sys.executable,
            "-c",
            "import sys; print('out', flush=True); print('err', file=sys.stderr, flush=True)",
        ],
        max_line_bytes=100,
    )
    lines = []
    try:
        while True:
            line = stream.receive(1)
            if line is not None:
                lines.append(line)
    except CommandStreamEnded as ended:
        assert ended.returncode == 0
    finally:
        stream.close()

    assert {(line.stream, line.payload) for line in lines} == {
        (CommandOutputStream.STDOUT, b"out"),
        (CommandOutputStream.STDERR, b"err"),
    }


def test_command_stream_bounds_and_drains_an_oversized_line() -> None:
    stream = SubprocessCommandStreamRunner().open(
        [sys.executable, "-c", "print('x' * 200, flush=True); print('tail', flush=True)"],
        max_line_bytes=32,
    )
    first = stream.receive(1)
    second = stream.receive(1)
    stream.close()

    assert first is not None
    assert first.payload == b"x" * 32
    assert first.truncated is True
    assert second is not None
    assert second.payload == b"tail"
    assert second.truncated is False


def test_closing_command_stream_terminates_the_process_group() -> None:
    stream = SubprocessCommandStreamRunner().open(
        [
            sys.executable,
            "-c",
            "import os,time; print(os.getpid(), flush=True); time.sleep(30)",
        ],
        max_line_bytes=100,
    )
    line = stream.receive(1)
    assert line is not None
    process_id = int(line.payload)

    stream.close()

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail("stream child process remained alive after close")
