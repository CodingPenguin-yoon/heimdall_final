from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from psycopg.errors import UniqueViolation

from heimdall.database import Database
from heimdall.projects.models import (
    Project,
    ProjectConflictError,
    ProjectEnvironmentSecret,
    ProjectNotFoundError,
    ProjectStatus,
    ProjectVersionConflictError,
)


class ProjectRepository(Protocol):
    def create(self, name: str, repository_url: str) -> Project: ...

    def list(self) -> Sequence[Project]: ...

    def get(self, project_id: UUID) -> Project: ...

    def get_environment_secret(
        self, project_id: UUID, service_name: str, variable_name: str
    ) -> ProjectEnvironmentSecret | None: ...

    def update_settings(
        self,
        project_id: UUID,
        expected_version: int,
        deployment_config: dict,
        environment_secrets: Sequence[ProjectEnvironmentSecret],
    ) -> Project: ...


class PostgresProjectRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create(self, name: str, repository_url: str) -> Project:
        now = datetime.now(UTC)
        try:
            with self._database.connection() as connection:
                row = connection.execute(
                    """
                    INSERT INTO projects (
                        id, name, repository_url, branch, status,
                        config_version, deployment_config, created_at, updated_at
                    ) VALUES (%s, %s, %s, 'main', 'DRAFT', 0, NULL, %s, %s)
                    RETURNING *
                    """,
                    (uuid4(), name, repository_url, now, now),
                ).fetchone()
        except UniqueViolation as error:
            raise ProjectConflictError from error
        return _project(row)

    def list(self) -> Sequence[Project]:
        with self._database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM projects ORDER BY updated_at DESC, id ASC"
            ).fetchall()
        return [_project(row) for row in rows]

    def get(self, project_id: UUID) -> Project:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE id = %s", (project_id,)
            ).fetchone()
        if row is None:
            raise ProjectNotFoundError
        return _project(row)

    def get_environment_secret(
        self, project_id: UUID, service_name: str, variable_name: str
    ) -> ProjectEnvironmentSecret | None:
        with self._database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM project_environment_secrets
                WHERE project_id = %s AND service_name = %s AND variable_name = %s
                """,
                (project_id, service_name, variable_name),
            ).fetchone()
        return _environment_secret(row) if row is not None else None

    def update_settings(
        self,
        project_id: UUID,
        expected_version: int,
        deployment_config: dict,
        environment_secrets: Sequence[ProjectEnvironmentSecret],
    ) -> Project:
        now = datetime.now(UTC)
        with self._database.connection() as connection:
            row = connection.execute(
                """
                UPDATE projects
                SET deployment_config = %s::jsonb,
                    config_version = config_version + 1,
                    status = 'READY',
                    updated_at = %s
                WHERE id = %s AND config_version = %s
                RETURNING *
                """,
                (json.dumps(deployment_config), now, project_id, expected_version),
            ).fetchone()
            if row is not None:
                active_keys = {
                    (secret.service_name, secret.variable_name) for secret in environment_secrets
                }
                existing_keys = {
                    (item["service_name"], item["variable_name"])
                    for item in connection.execute(
                        """
                        SELECT service_name, variable_name
                        FROM project_environment_secrets WHERE project_id = %s
                        """,
                        (project_id,),
                    ).fetchall()
                }
                for service_name, variable_name in existing_keys - active_keys:
                    connection.execute(
                        """
                        DELETE FROM project_environment_secrets
                        WHERE project_id = %s AND service_name = %s AND variable_name = %s
                        """,
                        (project_id, service_name, variable_name),
                    )
                for secret in environment_secrets:
                    connection.execute(
                        """
                        INSERT INTO project_environment_secrets (
                            project_id, service_name, variable_name, secret_reference,
                            secret_version, secret_fingerprint, created_at, updated_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (project_id, service_name, variable_name) DO UPDATE
                        SET secret_reference = EXCLUDED.secret_reference,
                            secret_version = EXCLUDED.secret_version,
                            secret_fingerprint = EXCLUDED.secret_fingerprint,
                            updated_at = EXCLUDED.updated_at
                        """,
                        (
                            secret.project_id,
                            secret.service_name,
                            secret.variable_name,
                            secret.secret_reference,
                            secret.secret_version,
                            secret.secret_fingerprint,
                            secret.created_at,
                            secret.updated_at,
                        ),
                    )
                return _project(row)
            exists = connection.execute(
                "SELECT 1 FROM projects WHERE id = %s", (project_id,)
            ).fetchone()
        if exists is None:
            raise ProjectNotFoundError
        raise ProjectVersionConflictError


def _project(row: dict) -> Project:
    return Project(
        id=row["id"],
        name=row["name"],
        repository_url=row["repository_url"],
        branch=row["branch"],
        status=ProjectStatus(row["status"]),
        config_version=row["config_version"],
        deployment_config=row["deployment_config"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _environment_secret(row: dict) -> ProjectEnvironmentSecret:
    return ProjectEnvironmentSecret(
        project_id=row["project_id"],
        service_name=row["service_name"],
        variable_name=row["variable_name"],
        secret_reference=row["secret_reference"],
        secret_version=row["secret_version"],
        secret_fingerprint=row["secret_fingerprint"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
