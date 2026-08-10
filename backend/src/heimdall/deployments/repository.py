from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

from psycopg.errors import UniqueViolation

from heimdall.database import Database
from heimdall.deployments.models import (
    ActiveDeploymentError,
    Deployment,
    DeploymentClaimLostError,
    DeploymentEvent,
    DeploymentJobClaim,
    DeploymentNotFoundError,
    DeploymentReconciliationConflictError,
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

    def list_recent(self, limit: int = 100) -> Sequence[Deployment]: ...

    def list_uncertain_before(self, cutoff: datetime, limit: int = 100) -> Sequence[Deployment]: ...

    def get(self, deployment_id: UUID) -> Deployment: ...

    def claim_next(
        self, worker_id: str, lease_duration: timedelta
    ) -> DeploymentJobClaim | None: ...

    def renew(self, claim: DeploymentJobClaim, lease_duration: timedelta) -> datetime: ...

    def advance(
        self,
        claim: DeploymentJobClaim,
        status: DeploymentStatus,
        *,
        event_code: str,
        event_message: str,
    ) -> Deployment: ...

    def succeed(self, claim: DeploymentJobClaim) -> Deployment: ...

    def fail(self, claim: DeploymentJobClaim, stage: str, code: str) -> Deployment: ...

    def retry(
        self, claim: DeploymentJobClaim, available_at: datetime, stage: str, code: str
    ) -> Deployment: ...

    def list_events(self, deployment_id: UUID, limit: int = 100) -> Sequence[DeploymentEvent]: ...

    def reconcile_succeeded(self, deployment_id: UUID) -> Deployment: ...


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

    def list_recent(self, limit: int = 100) -> Sequence[Deployment]:
        with self._database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM deployments
                ORDER BY created_at DESC, id ASC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
        return [_deployment(row) for row in rows]

    def list_uncertain_before(self, cutoff: datetime, limit: int = 100) -> Sequence[Deployment]:
        with self._database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM deployments
                WHERE status = 'FAILED'
                  AND failure_stage = 'RECOVERY'
                  AND failure_code = 'RECOVERY_STATE_UNCERTAIN'
                  AND terminal_at IS NOT NULL
                  AND terminal_at <= %s
                ORDER BY terminal_at, id
                LIMIT %s
                """,
                (cutoff, limit),
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

    def claim_next(self, worker_id: str, lease_duration: timedelta) -> DeploymentJobClaim | None:
        now = datetime.now(UTC)
        lease_expires_at = now + lease_duration
        token = uuid4()
        with self._database.connection() as connection:
            job = connection.execute(
                """
                WITH candidate AS (
                    SELECT job.deployment_id
                    FROM deployment_jobs AS job
                    JOIN deployments AS deployment ON deployment.id = job.deployment_id
                    WHERE (
                        (job.state = 'PENDING' AND job.available_at <= %s)
                        OR
                        (job.state = 'CLAIMED' AND job.lease_expires_at <= %s)
                    )
                    AND deployment.status NOT IN ('SUCCEEDED', 'FAILED')
                    ORDER BY job.available_at, job.created_at, job.deployment_id
                    FOR UPDATE OF job SKIP LOCKED
                    LIMIT 1
                )
                UPDATE deployment_jobs AS job
                SET state = 'CLAIMED',
                    attempts = job.attempts + 1,
                    lease_owner = %s,
                    lease_expires_at = %s,
                    claim_token = %s,
                    updated_at = %s
                FROM candidate
                WHERE job.deployment_id = candidate.deployment_id
                RETURNING job.*
                """,
                (now, now, worker_id, lease_expires_at, token, now),
            ).fetchone()
            if job is None:
                return None
            deployment_row = connection.execute(
                """
                UPDATE deployments
                SET status = 'PREPARING', failure_stage = NULL, failure_code = NULL,
                    updated_at = %s, terminal_at = NULL
                WHERE id = %s
                RETURNING *
                """,
                (now, job["deployment_id"]),
            ).fetchone()
            self._insert_event(
                connection,
                job["deployment_id"],
                DeploymentStatus.PREPARING.value,
                "JOB_CLAIMED",
                "Worker claimed the deployment job",
                now,
            )
        return DeploymentJobClaim(
            deployment=_deployment(deployment_row),
            token=token,
            worker_id=worker_id,
            attempts=job["attempts"],
            lease_expires_at=lease_expires_at,
        )

    def renew(self, claim: DeploymentJobClaim, lease_duration: timedelta) -> datetime:
        now = datetime.now(UTC)
        lease_expires_at = now + lease_duration
        with self._database.connection() as connection:
            row = connection.execute(
                """
                UPDATE deployment_jobs
                SET lease_expires_at = %s, updated_at = %s
                WHERE deployment_id = %s
                  AND state = 'CLAIMED'
                  AND lease_owner = %s
                  AND claim_token = %s
                  AND lease_expires_at > %s
                RETURNING lease_expires_at
                """,
                (
                    lease_expires_at,
                    now,
                    claim.deployment.id,
                    claim.worker_id,
                    claim.token,
                    now,
                ),
            ).fetchone()
        if row is None:
            raise DeploymentClaimLostError
        return row["lease_expires_at"]

    def advance(
        self,
        claim: DeploymentJobClaim,
        status: DeploymentStatus,
        *,
        event_code: str,
        event_message: str,
    ) -> Deployment:
        if status in {DeploymentStatus.QUEUED, DeploymentStatus.SUCCEEDED, DeploymentStatus.FAILED}:
            raise ValueError("advance requires a non-terminal processing status")
        now = datetime.now(UTC)
        with self._database.connection() as connection:
            self._lock_claim(connection, claim, now)
            row = connection.execute(
                """
                UPDATE deployments
                SET status = %s, updated_at = %s
                WHERE id = %s AND status NOT IN ('SUCCEEDED', 'FAILED')
                RETURNING *
                """,
                (status.value, now, claim.deployment.id),
            ).fetchone()
            if row is None:
                raise DeploymentClaimLostError
            self._insert_event(
                connection,
                claim.deployment.id,
                status.value,
                event_code,
                event_message,
                now,
            )
        return _deployment(row)

    def succeed(self, claim: DeploymentJobClaim) -> Deployment:
        now = datetime.now(UTC)
        with self._database.connection() as connection:
            self._lock_claim(connection, claim, now)
            row = connection.execute(
                """
                UPDATE deployments
                SET status = 'SUCCEEDED', failure_stage = NULL, failure_code = NULL,
                    updated_at = %s, terminal_at = %s
                WHERE id = %s AND status NOT IN ('SUCCEEDED', 'FAILED')
                RETURNING *
                """,
                (now, now, claim.deployment.id),
            ).fetchone()
            if row is None:
                raise DeploymentClaimLostError
            self._complete_job(connection, claim, now)
            self._insert_event(
                connection,
                claim.deployment.id,
                DeploymentStatus.SUCCEEDED.value,
                "DEPLOYMENT_SUCCEEDED",
                "The preview deployment is active",
                now,
            )
        return _deployment(row)

    def fail(self, claim: DeploymentJobClaim, stage: str, code: str) -> Deployment:
        now = datetime.now(UTC)
        with self._database.connection() as connection:
            self._lock_claim(connection, claim, now)
            row = connection.execute(
                """
                UPDATE deployments
                SET status = 'FAILED', failure_stage = %s, failure_code = %s,
                    updated_at = %s, terminal_at = %s
                WHERE id = %s AND status NOT IN ('SUCCEEDED', 'FAILED')
                RETURNING *
                """,
                (stage, code, now, now, claim.deployment.id),
            ).fetchone()
            if row is None:
                raise DeploymentClaimLostError
            self._complete_job(connection, claim, now)
            self._insert_event(
                connection,
                claim.deployment.id,
                DeploymentStatus.FAILED.value,
                code,
                "The deployment failed during runtime processing",
                now,
            )
        return _deployment(row)

    def retry(
        self, claim: DeploymentJobClaim, available_at: datetime, stage: str, code: str
    ) -> Deployment:
        now = datetime.now(UTC)
        with self._database.connection() as connection:
            self._lock_claim(connection, claim, now)
            row = connection.execute(
                """
                UPDATE deployments
                SET status = 'QUEUED', failure_stage = %s, failure_code = %s,
                    updated_at = %s, terminal_at = NULL
                WHERE id = %s AND status NOT IN ('SUCCEEDED', 'FAILED')
                RETURNING *
                """,
                (stage, code, now, claim.deployment.id),
            ).fetchone()
            if row is None:
                raise DeploymentClaimLostError
            updated = connection.execute(
                """
                UPDATE deployment_jobs
                SET state = 'PENDING', available_at = %s, lease_owner = NULL,
                    lease_expires_at = NULL, claim_token = NULL, updated_at = %s
                WHERE deployment_id = %s AND lease_owner = %s AND claim_token = %s
                """,
                (available_at, now, claim.deployment.id, claim.worker_id, claim.token),
            )
            if updated.rowcount != 1:
                raise DeploymentClaimLostError
            self._insert_event(
                connection,
                claim.deployment.id,
                DeploymentStatus.QUEUED.value,
                "DEPLOYMENT_RETRY_SCHEDULED",
                "A retryable runtime failure was scheduled for retry",
                now,
            )
        return _deployment(row)

    def list_events(self, deployment_id: UUID, limit: int = 100) -> Sequence[DeploymentEvent]:
        with self._database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM deployment_events
                WHERE deployment_id = %s
                ORDER BY id DESC
                LIMIT %s
                """,
                (deployment_id, limit),
            ).fetchall()
        return [_event(row) for row in reversed(rows)]

    def reconcile_succeeded(self, deployment_id: UUID) -> Deployment:
        now = datetime.now(UTC)
        with self._database.connection() as connection:
            current = connection.execute(
                "SELECT * FROM deployments WHERE id = %s FOR UPDATE",
                (deployment_id,),
            ).fetchone()
            if current is None:
                raise DeploymentNotFoundError
            if current["status"] == DeploymentStatus.SUCCEEDED.value:
                return _deployment(current)
            if not (
                current["status"] == DeploymentStatus.FAILED.value
                and current["failure_stage"] == "RECOVERY"
                and current["failure_code"] == "RECOVERY_STATE_UNCERTAIN"
            ):
                raise DeploymentReconciliationConflictError
            row = connection.execute(
                """
                UPDATE deployments
                SET status = 'SUCCEEDED', failure_stage = NULL, failure_code = NULL,
                    updated_at = %s, terminal_at = %s
                WHERE id = %s
                RETURNING *
                """,
                (now, now, deployment_id),
            ).fetchone()
            self._insert_event(
                connection,
                deployment_id,
                DeploymentStatus.SUCCEEDED.value,
                "DEPLOYMENT_RECONCILED_ACTIVE",
                "The preserved runtime was verified and restored as the active deployment",
                now,
            )
        return _deployment(row)

    @staticmethod
    def _lock_claim(connection, claim: DeploymentJobClaim, now: datetime) -> None:
        row = connection.execute(
            """
            SELECT deployment_id
            FROM deployment_jobs
            WHERE deployment_id = %s
              AND state = 'CLAIMED'
              AND lease_owner = %s
              AND claim_token = %s
              AND lease_expires_at > %s
            FOR UPDATE
            """,
            (claim.deployment.id, claim.worker_id, claim.token, now),
        ).fetchone()
        if row is None:
            raise DeploymentClaimLostError

    @staticmethod
    def _complete_job(connection, claim: DeploymentJobClaim, now: datetime) -> None:
        updated = connection.execute(
            """
            UPDATE deployment_jobs
            SET state = 'DONE', lease_owner = NULL, lease_expires_at = NULL,
                claim_token = NULL, updated_at = %s
            WHERE deployment_id = %s AND lease_owner = %s AND claim_token = %s
            """,
            (now, claim.deployment.id, claim.worker_id, claim.token),
        )
        if updated.rowcount != 1:
            raise DeploymentClaimLostError

    @staticmethod
    def _insert_event(
        connection,
        deployment_id: UUID,
        stage: str,
        code: str,
        message: str,
        created_at: datetime,
    ) -> None:
        connection.execute(
            """
            INSERT INTO deployment_events (
                deployment_id, stage, code, message, created_at
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            (deployment_id, stage, code, message, created_at),
        )


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


def _event(row: dict) -> DeploymentEvent:
    return DeploymentEvent(
        id=row["id"],
        deployment_id=row["deployment_id"],
        stage=row["stage"],
        code=row["code"],
        message=row["message"],
        created_at=row["created_at"],
    )
