from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from heimdall.database import Database
from heimdall.project_database.models import (
    ProjectDatabasePhase,
    ProjectDatabaseResource,
    ProjectDatabaseStatus,
    ProjectDatabaseVersionConflict,
)
from heimdall.secrets.store import StoredSecret


class ProjectDatabaseRepository(Protocol):
    def get_for_project(self, project_id: UUID) -> ProjectDatabaseResource | None: ...

    def ensure_intent(self, project_id: UUID) -> ProjectDatabaseResource: ...

    def begin_retry(self, resource: ProjectDatabaseResource) -> ProjectDatabaseResource: ...

    def record_secret(
        self, resource: ProjectDatabaseResource, secret: StoredSecret
    ) -> ProjectDatabaseResource: ...

    def advance(
        self, resource: ProjectDatabaseResource, phase: ProjectDatabasePhase
    ) -> ProjectDatabaseResource: ...

    def mark_failed(
        self, resource: ProjectDatabaseResource, stage: str, code: str
    ) -> ProjectDatabaseResource: ...


class PostgresProjectDatabaseRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def get_for_project(self, project_id: UUID) -> ProjectDatabaseResource | None:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM project_database_resources WHERE project_id = %s",
                (project_id,),
            ).fetchone()
        return _resource(row) if row is not None else None

    def ensure_intent(self, project_id: UUID) -> ProjectDatabaseResource:
        existing = self.get_for_project(project_id)
        if existing is not None:
            return existing
        resource_id = uuid4()
        suffix = resource_id.hex
        now = datetime.now(UTC)
        with self._database.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO project_database_resources (
                    id, project_id, desired_state, status, phase,
                    database_name, role_name, schema_name, created_at, updated_at
                ) VALUES (
                    %s, %s, 'ACTIVE', 'PROVISIONING', 'INTENT_RECORDED',
                    %s, %s, 'app', %s, %s
                )
                ON CONFLICT (project_id) DO NOTHING
                RETURNING *
                """,
                (
                    resource_id,
                    project_id,
                    f"hd_db_{suffix}",
                    f"hd_role_{suffix}",
                    now,
                    now,
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT * FROM project_database_resources WHERE project_id = %s",
                    (project_id,),
                ).fetchone()
        return _resource(row)

    def begin_retry(self, resource: ProjectDatabaseResource) -> ProjectDatabaseResource:
        return self._update(
            resource,
            "status = 'PROVISIONING', failure_stage = NULL, failure_code = NULL",
            (),
        )

    def record_secret(
        self, resource: ProjectDatabaseResource, secret: StoredSecret
    ) -> ProjectDatabaseResource:
        return self._update(
            resource,
            """
            credential_reference = %s, credential_version = %s,
            credential_fingerprint = %s, phase = 'SECRET_READY'
            """,
            (secret.reference, secret.version, secret.fingerprint),
        )

    def advance(
        self, resource: ProjectDatabaseResource, phase: ProjectDatabasePhase
    ) -> ProjectDatabaseResource:
        if phase is ProjectDatabasePhase.ACTIVE:
            return self._update(resource, "phase = %s, status = 'ACTIVE'", (phase.value,))
        return self._update(resource, "phase = %s", (phase.value,))

    def mark_failed(
        self, resource: ProjectDatabaseResource, stage: str, code: str
    ) -> ProjectDatabaseResource:
        return self._update(
            resource,
            "status = 'FAILED', failure_stage = %s, failure_code = %s",
            (stage, code),
        )

    def _update(
        self, resource: ProjectDatabaseResource, assignments: str, values: tuple
    ) -> ProjectDatabaseResource:
        now = datetime.now(UTC)
        with self._database.connection() as connection:
            row = connection.execute(
                f"""
                UPDATE project_database_resources
                SET {assignments}, state_version = state_version + 1, updated_at = %s
                WHERE id = %s AND state_version = %s
                RETURNING *
                """,
                (*values, now, resource.id, resource.state_version),
            ).fetchone()
        if row is None:
            raise ProjectDatabaseVersionConflict
        return _resource(row)


def _resource(row: dict) -> ProjectDatabaseResource:
    return ProjectDatabaseResource(
        id=row["id"],
        project_id=row["project_id"],
        desired_state=row["desired_state"],
        status=ProjectDatabaseStatus(row["status"]),
        phase=ProjectDatabasePhase(row["phase"]),
        database_name=row["database_name"],
        role_name=row["role_name"],
        schema_name=row["schema_name"],
        credential_reference=row["credential_reference"],
        credential_version=row["credential_version"],
        credential_fingerprint=row["credential_fingerprint"],
        state_version=row["state_version"],
        failure_stage=row["failure_stage"],
        failure_code=row["failure_code"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
