from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    runtime_root: Path
    git_executable: str
    git_workspace_root: Path
    git_timeout_seconds: float
    recent_commit_limit: int
    project_database_enabled: bool
    project_database_admin_url: str | None
    project_database_runtime_host: str
    project_database_runtime_port: int

    @classmethod
    def from_environment(cls) -> Settings:
        workspace = Path(
            os.environ.get("HEIMDALL_GIT_WORKSPACE_ROOT", "/tmp/heimdall-python-git")
        ).resolve()
        return cls(
            database_url=os.environ.get(
                "HEIMDALL_DATABASE_URL",
                "postgresql://heimdall:change-me@127.0.0.1:55432/heimdall",
            ),
            runtime_root=Path(
                os.environ.get("HEIMDALL_RUNTIME_ROOT", "/tmp/heimdall-python-runtime")
            ).resolve(),
            git_executable=os.environ.get("HEIMDALL_GIT_EXECUTABLE", "git"),
            git_workspace_root=workspace,
            git_timeout_seconds=float(os.environ.get("HEIMDALL_GIT_TIMEOUT_SECONDS", "20")),
            recent_commit_limit=int(os.environ.get("HEIMDALL_RECENT_COMMIT_LIMIT", "20")),
            project_database_enabled=os.environ.get("HEIMDALL_PROJECT_DB_ENABLED", "false").lower()
            in {"1", "true", "yes"},
            project_database_admin_url=os.environ.get("HEIMDALL_PROJECT_DB_ADMIN_URL") or None,
            project_database_runtime_host=os.environ.get(
                "HEIMDALL_PROJECT_DB_RUNTIME_HOST", "managed-postgres"
            ),
            project_database_runtime_port=int(
                os.environ.get("HEIMDALL_PROJECT_DB_RUNTIME_PORT", "5432")
            ),
        )
