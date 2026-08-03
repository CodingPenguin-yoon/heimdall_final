from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from psycopg.errors import UniqueViolation

from heimdall.database import Database
from heimdall.deployments.models import (
    ActiveDeploymentError,
    Deployment,
    DeploymentNotFoundError,
    DeploymentSource,
    DeploymentStatus,
)


class DeploymentRepository(Protocol):
    def create(
        self,
        *,
        project_id: UUID,
        source_type: DeploymentSource,
        requested_commit_sha: str | None,
        resolved_commit_sha: str,
        config_version: int,
        config_snapshot: dict[str, Any],
    ) -> Deployment: ...

    def list_for_project(self, project_id: UUID) -> Sequence[Deployment]: ...

    def get(self, deployment_id: UUID) -> Deployment: ...


class PostgresDeploymentRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create(
        self,
        *,
        project_id: UUID,
        source_type: DeploymentSource,
        requested_commit_sha: str | None,
        resolved_commit_sha: str,
        config_version: int,
        config_snapshot: dict[str, Any],
    ) -> Deployment:
        deployment_id = uuid4()
        now = datetime.now(UTC)
        try:
            with self._database.connection() as connection:
                row = connection.execute(
                    """
                    INSERT INTO deployments (
                        id, project_id, source_type, requested_commit_sha,
                        resolved_commit_sha, config_version, config_snapshot,
                        status, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, 'QUEUED', %s, %s)
                    RETURNING *
                    """,
                    (
                        deployment_id,
                        project_id,
                        source_type.value,
                        requested_commit_sha,
                        resolved_commit_sha,
                        config_version,
                        json.dumps(config_snapshot),
                        now,
                        now,
                    ),
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO deployment_jobs (
                        deployment_id, state, available_at, created_at, updated_at
                    ) VALUES (%s, 'PENDING', %s, %s, %s)
                    """,
                    (deployment_id, now, now, now),
                )
        except UniqueViolation as error:
            raise ActiveDeploymentError from error
        return _deployment(row)

    def list_for_project(self, project_id: UUID) -> Sequence[Deployment]:
        with self._database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM deployments
                WHERE project_id = %s
                ORDER BY created_at DESC, id ASC
                LIMIT 50
                """,
                (project_id,),
            ).fetchall()
        return [_deployment(row) for row in rows]

    def get(self, deployment_id: UUID) -> Deployment:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM deployments WHERE id = %s", (deployment_id,)
            ).fetchone()
        if row is None:
            raise DeploymentNotFoundError
        return _deployment(row)


def _deployment(row: dict) -> Deployment:
    return Deployment(
        id=row["id"],
        project_id=row["project_id"],
        source_type=DeploymentSource(row["source_type"]),
        requested_commit_sha=row["requested_commit_sha"],
        resolved_commit_sha=row["resolved_commit_sha"],
        config_version=row["config_version"],
        config_snapshot=row["config_snapshot"],
        status=DeploymentStatus(row["status"]),
        failure_stage=row["failure_stage"],
        failure_code=row["failure_code"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        terminal_at=row["terminal_at"],
    )
