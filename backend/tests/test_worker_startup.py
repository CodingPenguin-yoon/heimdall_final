from __future__ import annotations

import os
from threading import Event
from types import SimpleNamespace

import pytest

from heimdall import worker


def test_worker_prepares_exact_private_roots_before_opening_database(tmp_path, monkeypatch) -> None:
    runtime_root = tmp_path / "runtime"
    git_root = tmp_path / "git"
    runtime_root.mkdir(mode=0o755)
    git_root.mkdir(mode=0o755)
    untouched = runtime_root / "projects" / "existing"
    untouched.mkdir(mode=0o755, parents=True)

    class DatabaseObserved(Exception):
        pass

    class Database:
        def __init__(self, url) -> None:
            pass

        def open(self) -> None:
            expected = (
                runtime_root,
                runtime_root / ".locks",
                runtime_root / ".locks" / "projects",
                runtime_root / "gateways",
                git_root,
            )
            for path in expected:
                assert path.stat().st_mode & 0o777 == 0o700
                assert path.stat().st_uid == os.geteuid()
                assert path.stat().st_gid == os.getegid()
            assert untouched.stat().st_mode & 0o777 == 0o755
            raise DatabaseObserved

    monkeypatch.setattr(worker, "Database", Database)
    settings = SimpleNamespace(
        database_url="postgresql://unused",
        runtime_root=runtime_root,
        git_workspace_root=git_root,
    )

    with pytest.raises(DatabaseObserved):
        worker.run(settings, Event())
