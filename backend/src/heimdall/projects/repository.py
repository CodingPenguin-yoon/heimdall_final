from __future__ import annotations

import json
from collections.abc import Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from psycopg.errors import UniqueViolation

from heimdall.database import Database
from heimdall.deployments.models import Deployment
from heimdall.deployments.repository import _deployment
from heimdall.projects.models import (
    Project,
    ProjectConflictError,
    ProjectDeletionClaimLostError,
    ProjectDeletionConflictError,
    ProjectDeletionJob,
    ProjectDeletionJobClaim,
    ProjectDeletionNotFoundError,
    ProjectDeletionPhase,
    ProjectDeletionState,
    ProjectDeletionValidationError,
    ProjectEnvironmentSecret,
    ProjectNotFoundError,
    ProjectStatus,
    ProjectVersionConflictError,
)
from heimdall.runtime.repository import ProjectRuntime, _runtime


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

    def lock_for_external_operation(self, project_id: UUID) -> AbstractContextManager[Project]: ...

    def request_deletion(
        self,
        project_id: UUID,
        *,
        confirmation: str,
        delete_managed_database: bool,
        managed_database_confirmation: str | None,
    ) -> ProjectDeletionJob: ...

    def get_deletion(self, project_id: UUID) -> ProjectDeletionJob: ...

    def retry_deletion(
        self,
        project_id: UUID,
        *,
        confirmation: str,
        delete_managed_database: bool,
        managed_database_confirmation: str | None,
    ) -> ProjectDeletionJob: ...

    def claim_next_deletion(
        self, worker_id: str, lease_duration: timedelta
    ) -> ProjectDeletionJobClaim | None: ...

    def renew_deletion(
        self, claim: ProjectDeletionJobClaim, lease_duration: timedelta
    ) -> datetime: ...

    def advance_deletion(
        self,
        claim: ProjectDeletionJobClaim,
        expected_phase: ProjectDeletionPhase,
        next_phase: ProjectDeletionPhase,
    ) -> ProjectDeletionJob: ...

    def fail_deletion(
        self, claim: ProjectDeletionJobClaim, code: str, *, retryable: bool
    ) -> ProjectDeletionJob: ...

    def reschedule_deletion(
        self, claim: ProjectDeletionJobClaim, available_at: datetime, code: str
    ) -> ProjectDeletionJob: ...

    def defer_deletion(
        self, claim: ProjectDeletionJobClaim, available_at: datetime
    ) -> ProjectDeletionJob: ...

    def deletion_operations_drained(self, project_id: UUID) -> bool: ...

    def deletion_runtime_snapshot(
        self, project_id: UUID
    ) -> tuple[Sequence[Deployment], ProjectRuntime | None]: ...

    def deletion_mutation_allowed(
        self, claim: ProjectDeletionJobClaim, *, require_route_removed: bool
    ) -> bool: ...

    def finalize_deletion(self, claim: ProjectDeletionJobClaim) -> None: ...


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
                """
                SELECT project.*,
                       EXISTS (
                           SELECT 1 FROM project_database_resources AS resource
                           WHERE resource.project_id = project.id
                       ) AS has_managed_database
                FROM projects AS project
                ORDER BY project.updated_at DESC, project.id ASC
                """
            ).fetchall()
        return [_project(row) for row in rows]

    def get(self, project_id: UUID) -> Project:
        with self._database.connection() as connection:
            row = connection.execute(
                """
                SELECT project.*,
                       EXISTS (
                           SELECT 1 FROM project_database_resources AS resource
                           WHERE resource.project_id = project.id
                       ) AS has_managed_database
                FROM projects AS project
                WHERE project.id = %s
                """,
                (project_id,),
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

    @contextmanager
    def lock_for_external_operation(self, project_id: UUID):
        with self._database.connection() as connection:
            row = connection.execute(
                """
                SELECT project.*,
                       EXISTS (
                           SELECT 1 FROM project_database_resources AS resource
                           WHERE resource.project_id = project.id
                       ) AS has_managed_database
                FROM projects AS project
                WHERE project.id = %s
                FOR UPDATE OF project
                """,
                (project_id,),
            ).fetchone()
            if row is None:
                raise ProjectNotFoundError
            yield _project(row)

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
                WHERE id = %s AND config_version = %s AND status <> 'DELETING'
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
            existing = connection.execute(
                "SELECT status FROM projects WHERE id = %s", (project_id,)
            ).fetchone()
        if existing is None:
            raise ProjectNotFoundError
        if existing["status"] == ProjectStatus.DELETING.value:
            raise ProjectDeletionConflictError("PROJECT_DELETING")
        raise ProjectVersionConflictError

    def request_deletion(
        self,
        project_id: UUID,
        *,
        confirmation: str,
        delete_managed_database: bool,
        managed_database_confirmation: str | None,
    ) -> ProjectDeletionJob:
        now = datetime.now(UTC)
        with self._database.connection() as connection:
            _, has_database = self._lock_deletion_context(connection, project_id)
            self._validate_deletion_confirmation(
                project_id,
                confirmation,
                has_database,
                delete_managed_database,
                managed_database_confirmation,
            )
            existing = connection.execute(
                "SELECT * FROM project_deletion_jobs WHERE project_id = %s FOR UPDATE",
                (project_id,),
            ).fetchone()
            if existing is not None:
                if existing["state"] == ProjectDeletionState.FAILED.value:
                    raise ProjectDeletionConflictError("PROJECT_DELETION_FAILED")
                return _deletion_job(existing)
            connection.execute(
                "UPDATE projects SET status = 'DELETING', updated_at = %s WHERE id = %s",
                (now, project_id),
            )
            row = connection.execute(
                """
                INSERT INTO project_deletion_jobs (
                    project_id, state, phase, available_at, delete_managed_database,
                    created_at, updated_at
                ) VALUES (%s, 'PENDING', 'REQUESTED', %s, %s, %s, %s)
                RETURNING *
                """,
                (project_id, now, has_database, now, now),
            ).fetchone()
        return _deletion_job(row)

    def get_deletion(self, project_id: UUID) -> ProjectDeletionJob:
        with self._database.connection() as connection:
            project = connection.execute(
                "SELECT 1 FROM projects WHERE id = %s", (project_id,)
            ).fetchone()
            if project is None:
                raise ProjectNotFoundError
            row = connection.execute(
                "SELECT * FROM project_deletion_jobs WHERE project_id = %s",
                (project_id,),
            ).fetchone()
        if row is None:
            raise ProjectDeletionNotFoundError
        return _deletion_job(row)

    def retry_deletion(
        self,
        project_id: UUID,
        *,
        confirmation: str,
        delete_managed_database: bool,
        managed_database_confirmation: str | None,
    ) -> ProjectDeletionJob:
        now = datetime.now(UTC)
        with self._database.connection() as connection:
            _, has_database = self._lock_deletion_context(connection, project_id)
            self._validate_deletion_confirmation(
                project_id,
                confirmation,
                has_database,
                delete_managed_database,
                managed_database_confirmation,
            )
            row = connection.execute(
                """
                UPDATE project_deletion_jobs
                SET state = 'PENDING', attempts = 0, available_at = %s,
                    lease_owner = NULL, lease_expires_at = NULL, claim_token = NULL,
                    last_error_code = NULL, last_error_retryable = NULL, updated_at = %s
                WHERE project_id = %s AND state = 'FAILED'
                RETURNING *
                """,
                (now, now, project_id),
            ).fetchone()
            if row is None:
                exists = connection.execute(
                    "SELECT 1 FROM project_deletion_jobs WHERE project_id = %s",
                    (project_id,),
                ).fetchone()
                if exists is None:
                    raise ProjectDeletionNotFoundError
                raise ProjectDeletionConflictError("PROJECT_DELETION_NOT_FAILED")
        return _deletion_job(row)

    def claim_next_deletion(
        self, worker_id: str, lease_duration: timedelta
    ) -> ProjectDeletionJobClaim | None:
        token = uuid4()
        with self._database.connection() as connection:
            row = connection.execute(
                """
                WITH candidate AS (
                    SELECT project_id
                    FROM project_deletion_jobs
                    WHERE (state = 'PENDING' AND available_at <= clock_timestamp())
                       OR (state = 'CLAIMED' AND lease_expires_at <= clock_timestamp())
                    ORDER BY available_at, created_at, project_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE project_deletion_jobs AS job
                SET state = 'CLAIMED', attempts = job.attempts + 1,
                    lease_owner = %s,
                    lease_expires_at = clock_timestamp() + %s,
                    claim_token = %s, updated_at = clock_timestamp()
                FROM candidate
                WHERE job.project_id = candidate.project_id
                RETURNING job.*
                """,
                (worker_id, lease_duration, token),
            ).fetchone()
        if row is None:
            return None
        return ProjectDeletionJobClaim(
            job=_deletion_job(row),
            token=token,
            worker_id=worker_id,
            lease_expires_at=row["lease_expires_at"],
        )

    def renew_deletion(self, claim: ProjectDeletionJobClaim, lease_duration: timedelta) -> datetime:
        with self._database.connection() as connection:
            row = connection.execute(
                """
                UPDATE project_deletion_jobs
                SET lease_expires_at = clock_timestamp() + %s,
                    updated_at = clock_timestamp()
                WHERE project_id = %s AND state = 'CLAIMED'
                  AND lease_owner = %s AND claim_token = %s
                  AND lease_expires_at > clock_timestamp()
                RETURNING lease_expires_at
                """,
                (
                    lease_duration,
                    claim.job.project_id,
                    claim.worker_id,
                    claim.token,
                ),
            ).fetchone()
        if row is None:
            raise ProjectDeletionClaimLostError
        return row["lease_expires_at"]

    def advance_deletion(
        self,
        claim: ProjectDeletionJobClaim,
        expected_phase: ProjectDeletionPhase,
        next_phase: ProjectDeletionPhase,
    ) -> ProjectDeletionJob:
        phases = list(ProjectDeletionPhase)
        if phases.index(next_phase) != phases.index(expected_phase) + 1:
            raise ValueError("deletion phase must advance exactly one step")
        with self._database.connection() as connection:
            self._lock_deletion_claim(connection, claim)
            row = connection.execute(
                """
                UPDATE project_deletion_jobs
                SET phase = %s, updated_at = clock_timestamp()
                WHERE project_id = %s AND phase = %s
                RETURNING *
                """,
                (next_phase.value, claim.job.project_id, expected_phase.value),
            ).fetchone()
        if row is None:
            raise ProjectDeletionClaimLostError
        return _deletion_job(row)

    def fail_deletion(
        self, claim: ProjectDeletionJobClaim, code: str, *, retryable: bool
    ) -> ProjectDeletionJob:
        with self._database.connection() as connection:
            self._lock_deletion_claim(connection, claim)
            row = connection.execute(
                """
                UPDATE project_deletion_jobs
                SET state = 'FAILED', lease_owner = NULL, lease_expires_at = NULL,
                    claim_token = NULL, last_error_code = %s,
                    last_error_retryable = %s, updated_at = clock_timestamp()
                WHERE project_id = %s
                RETURNING *
                """,
                (code, retryable, claim.job.project_id),
            ).fetchone()
        return _deletion_job(row)

    def reschedule_deletion(
        self, claim: ProjectDeletionJobClaim, available_at: datetime, code: str
    ) -> ProjectDeletionJob:
        del code
        with self._database.connection() as connection:
            self._lock_deletion_claim(connection, claim)
            row = connection.execute(
                """
                UPDATE project_deletion_jobs
                SET state = 'PENDING', available_at = %s,
                    lease_owner = NULL, lease_expires_at = NULL, claim_token = NULL,
                    updated_at = clock_timestamp()
                WHERE project_id = %s
                RETURNING *
                """,
                (available_at, claim.job.project_id),
            ).fetchone()
        if row is None:
            raise ProjectDeletionClaimLostError
        return _deletion_job(row)

    def defer_deletion(
        self, claim: ProjectDeletionJobClaim, available_at: datetime
    ) -> ProjectDeletionJob:
        with self._database.connection() as connection:
            self._lock_deletion_claim(connection, claim)
            row = connection.execute(
                """
                UPDATE project_deletion_jobs
                SET state = 'PENDING', attempts = GREATEST(attempts - 1, 0),
                    available_at = %s, lease_owner = NULL,
                    lease_expires_at = NULL, claim_token = NULL,
                    updated_at = clock_timestamp()
                WHERE project_id = %s
                RETURNING *
                """,
                (available_at, claim.job.project_id),
            ).fetchone()
        if row is None:
            raise ProjectDeletionClaimLostError
        return _deletion_job(row)

    def deletion_operations_drained(self, project_id: UUID) -> bool:
        with self._database.connection() as connection:
            row = connection.execute(
                """
                SELECT
                    NOT EXISTS (
                        SELECT 1
                        FROM deployments AS deployment
                        LEFT JOIN deployment_jobs AS job
                          ON job.deployment_id = deployment.id
                        WHERE deployment.project_id = %s
                          AND (
                            deployment.status IN (
                                'QUEUED', 'PREPARING', 'BUILDING', 'STARTING',
                                'HEALTH_CHECKING', 'ACTIVATING'
                            )
                            OR job.state = 'CLAIMED'
                          )
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM public_route_jobs
                        WHERE project_id = %s AND state = 'CLAIMED'
                    )
                    AND NOT EXISTS (
                        SELECT 1
                        FROM runtime_reconciliations AS reconciliation
                        JOIN deployments AS deployment
                          ON deployment.id = reconciliation.deployment_id
                        WHERE deployment.project_id = %s
                          AND reconciliation.state IN ('PENDING', 'CLAIMED')
                    ) AS drained
                """,
                (project_id, project_id, project_id),
            ).fetchone()
        return bool(row and row["drained"])

    def deletion_runtime_snapshot(
        self, project_id: UUID
    ) -> tuple[Sequence[Deployment], ProjectRuntime | None]:
        with self._database.connection() as connection:
            deployment_rows = connection.execute(
                """
                SELECT * FROM deployments
                WHERE project_id = %s
                ORDER BY created_at, id
                """,
                (project_id,),
            ).fetchall()
            runtime_row = connection.execute(
                "SELECT * FROM project_runtimes WHERE project_id = %s",
                (project_id,),
            ).fetchone()
        return (
            tuple(_deployment(row) for row in deployment_rows),
            _runtime(runtime_row) if runtime_row is not None else None,
        )

    def deletion_mutation_allowed(
        self, claim: ProjectDeletionJobClaim, *, require_route_removed: bool
    ) -> bool:
        with self._database.connection() as connection:
            row = connection.execute(
                """
                SELECT job.project_id
                FROM project_deletion_jobs AS job
                JOIN projects AS project ON project.id = job.project_id
                LEFT JOIN project_public_routes AS route
                  ON route.project_id = job.project_id
                WHERE job.project_id = %s
                  AND job.state = 'CLAIMED'
                  AND job.lease_owner = %s
                  AND job.claim_token = %s
                  AND job.lease_expires_at > clock_timestamp()
                  AND project.status = 'DELETING'
                  AND (
                    NOT %s
                    OR route.project_id IS NULL
                    OR (
                      route.desired_state = 'DISABLED'
                      AND route.status = 'INACTIVE'
                      AND route.applied_revision = route.desired_revision
                      AND route.applied_hostname IS NULL
                    )
                  )
                """,
                (
                    claim.job.project_id,
                    claim.worker_id,
                    claim.token,
                    require_route_removed,
                ),
            ).fetchone()
        return row is not None

    def finalize_deletion(self, claim: ProjectDeletionJobClaim) -> None:
        project_id = claim.job.project_id
        with self._database.connection() as connection:
            row = connection.execute(
                """
                SELECT job.project_id
                FROM project_deletion_jobs AS job
                JOIN projects AS project ON project.id = job.project_id
                LEFT JOIN project_public_routes AS route
                  ON route.project_id = job.project_id
                WHERE job.project_id = %s
                  AND job.state = 'CLAIMED'
                  AND job.phase = 'METADATA_DELETE'
                  AND job.lease_owner = %s
                  AND job.claim_token = %s
                  AND job.lease_expires_at > clock_timestamp()
                  AND project.status = 'DELETING'
                  AND (
                    route.project_id IS NULL
                    OR (
                      route.desired_state = 'DISABLED'
                      AND route.status = 'INACTIVE'
                      AND route.applied_revision = route.desired_revision
                      AND route.applied_hostname IS NULL
                    )
                  )
                FOR UPDATE OF job, project
                """,
                (project_id, claim.worker_id, claim.token),
            ).fetchone()
            if row is None:
                raise ProjectDeletionClaimLostError
            connection.execute("DELETE FROM project_runtimes WHERE project_id = %s", (project_id,))
            connection.execute("DELETE FROM public_route_jobs WHERE project_id = %s", (project_id,))
            connection.execute(
                "DELETE FROM project_public_routes WHERE project_id = %s", (project_id,)
            )
            connection.execute(
                """
                DELETE FROM runtime_reconciliations
                WHERE deployment_id IN (
                    SELECT id FROM deployments WHERE project_id = %s
                )
                """,
                (project_id,),
            )
            connection.execute("DELETE FROM deployments WHERE project_id = %s", (project_id,))
            connection.execute(
                "DELETE FROM project_environment_secrets WHERE project_id = %s", (project_id,)
            )
            connection.execute(
                "DELETE FROM project_database_resources WHERE project_id = %s", (project_id,)
            )
            connection.execute(
                "DELETE FROM project_deletion_jobs WHERE project_id = %s", (project_id,)
            )
            result = connection.execute(
                "DELETE FROM projects WHERE id = %s RETURNING id", (project_id,)
            ).fetchone()
            if result is None:
                raise ProjectDeletionClaimLostError

    @staticmethod
    def _lock_deletion_context(connection, project_id: UUID) -> tuple[dict, bool]:
        project = connection.execute(
            "SELECT * FROM projects WHERE id = %s FOR UPDATE", (project_id,)
        ).fetchone()
        if project is None:
            raise ProjectNotFoundError
        database = connection.execute(
            "SELECT 1 FROM project_database_resources WHERE project_id = %s",
            (project_id,),
        ).fetchone()
        return project, database is not None

    @staticmethod
    def _validate_deletion_confirmation(
        project_id: UUID,
        confirmation: str,
        has_database: bool,
        delete_managed_database: bool,
        managed_database_confirmation: str | None,
    ) -> None:
        if confirmation != str(project_id):
            raise ProjectDeletionValidationError("PROJECT_DELETE_CONFIRMATION_MISMATCH")
        expected = f"DELETE {project_id} APPLICATION DATA"
        if has_database and (
            not delete_managed_database or managed_database_confirmation != expected
        ):
            raise ProjectDeletionValidationError("PROJECT_DATABASE_DELETE_CONFIRMATION_REQUIRED")

    @staticmethod
    def _lock_deletion_claim(connection, claim: ProjectDeletionJobClaim) -> None:
        row = connection.execute(
            """
            SELECT project_id FROM project_deletion_jobs
            WHERE project_id = %s AND state = 'CLAIMED'
              AND lease_owner = %s AND claim_token = %s
              AND lease_expires_at > clock_timestamp()
            FOR UPDATE
            """,
            (claim.job.project_id, claim.worker_id, claim.token),
        ).fetchone()
        if row is None:
            raise ProjectDeletionClaimLostError


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
        has_managed_database=row.get("has_managed_database", False),
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


def _deletion_job(row: dict) -> ProjectDeletionJob:
    return ProjectDeletionJob(
        project_id=row["project_id"],
        state=ProjectDeletionState(row["state"]),
        phase=ProjectDeletionPhase(row["phase"]),
        attempts=row["attempts"],
        available_at=row["available_at"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        claim_token=row["claim_token"],
        last_error_code=row["last_error_code"],
        last_error_retryable=row["last_error_retryable"],
        delete_managed_database=row["delete_managed_database"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
