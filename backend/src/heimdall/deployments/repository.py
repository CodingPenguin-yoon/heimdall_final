from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

from psycopg.errors import UniqueViolation

from heimdall.database import Database
from heimdall.deployments.diagnostics import (
    DIAGNOSTIC_ARTIFACT_MAX_BYTES,
    DeploymentDiagnosticArtifact,
    DeploymentDiagnosticNotFoundError,
    DiagnosticArtifactDraft,
    DiagnosticArtifactKind,
    DiagnosticCaptureStatus,
    DiagnosticLine,
    DiagnosticStream,
)
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

DEPLOYMENT_EVENT_CHANNEL = "heimdall_deployment_events"


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

    def list_events_after(
        self, deployment_id: UUID, after_id: int, limit: int = 100
    ) -> Sequence[DeploymentEvent]: ...

    def record_diagnostics(
        self,
        claim: DeploymentJobClaim,
        *,
        failure_stage: str,
        failure_code: str,
        artifacts: Sequence[DiagnosticArtifactDraft],
        retention: timedelta,
    ) -> DeploymentEvent: ...

    def record_reconciliation_diagnostics(
        self,
        deployment_id: UUID,
        *,
        failure_stage: str,
        failure_code: str,
        artifacts: Sequence[DiagnosticArtifactDraft],
        retention: timedelta,
    ) -> DeploymentEvent: ...

    def list_diagnostics(self, deployment_id: UUID) -> Sequence[DeploymentDiagnosticArtifact]: ...

    def get_diagnostic(
        self, deployment_id: UUID, artifact_id: UUID
    ) -> DeploymentDiagnosticArtifact: ...

    def purge_expired_diagnostics(self, limit: int = 100) -> int: ...

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

    def list_events_after(
        self, deployment_id: UUID, after_id: int, limit: int = 100
    ) -> Sequence[DeploymentEvent]:
        with self._database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM deployment_events
                WHERE deployment_id = %s AND id > %s
                ORDER BY id ASC
                LIMIT %s
                """,
                (deployment_id, after_id, limit),
            ).fetchall()
        return [_event(row) for row in rows]

    def record_diagnostics(
        self,
        claim: DeploymentJobClaim,
        *,
        failure_stage: str,
        failure_code: str,
        artifacts: Sequence[DiagnosticArtifactDraft],
        retention: timedelta,
    ) -> DeploymentEvent:
        if retention <= timedelta(0):
            raise ValueError("diagnostic retention must be positive")
        now = datetime.now(UTC)
        event_code, message = _diagnostic_event_summary(artifacts)
        with self._database.connection() as connection:
            self._lock_claim(connection, claim, now)
            event_id = self._insert_event(
                connection,
                claim.deployment.id,
                failure_stage,
                event_code,
                message,
                now,
            )
            self._insert_diagnostic_artifacts(
                connection,
                deployment_id=claim.deployment.id,
                event_id=event_id,
                failure_stage=failure_stage,
                failure_code=failure_code,
                artifacts=artifacts,
                retention=retention,
            )
        return DeploymentEvent(
            id=event_id,
            deployment_id=claim.deployment.id,
            stage=failure_stage,
            code=event_code,
            message=message,
            created_at=now,
        )

    def record_reconciliation_diagnostics(
        self,
        deployment_id: UUID,
        *,
        failure_stage: str,
        failure_code: str,
        artifacts: Sequence[DiagnosticArtifactDraft],
        retention: timedelta,
    ) -> DeploymentEvent:
        if retention <= timedelta(0):
            raise ValueError("diagnostic retention must be positive")
        now = datetime.now(UTC)
        event_code, message = _diagnostic_event_summary(artifacts)
        with self._database.connection() as connection:
            current = connection.execute(
                """
                SELECT status, failure_stage, failure_code
                FROM deployments
                WHERE id = %s
                FOR UPDATE
                """,
                (deployment_id,),
            ).fetchone()
            if current is None:
                raise DeploymentNotFoundError
            if not (
                current["status"] == DeploymentStatus.FAILED.value
                and current["failure_stage"] == "RECOVERY"
                and current["failure_code"] == "RECOVERY_STATE_UNCERTAIN"
            ):
                raise DeploymentReconciliationConflictError
            event_id = self._insert_event(
                connection,
                deployment_id,
                failure_stage,
                event_code,
                message,
                now,
            )
            self._insert_diagnostic_artifacts(
                connection,
                deployment_id=deployment_id,
                event_id=event_id,
                failure_stage=failure_stage,
                failure_code=failure_code,
                artifacts=artifacts,
                retention=retention,
            )
        return DeploymentEvent(
            id=event_id,
            deployment_id=deployment_id,
            stage=failure_stage,
            code=event_code,
            message=message,
            created_at=now,
        )

    def list_diagnostics(self, deployment_id: UUID) -> Sequence[DeploymentDiagnosticArtifact]:
        with self._database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM deployment_diagnostic_artifacts
                WHERE deployment_id = %s AND expires_at > %s
                ORDER BY event_id, captured_at, id
                """,
                (deployment_id, datetime.now(UTC)),
            ).fetchall()
        return [_diagnostic(row, include_lines=False) for row in rows]

    def get_diagnostic(
        self, deployment_id: UUID, artifact_id: UUID
    ) -> DeploymentDiagnosticArtifact:
        with self._database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM deployment_diagnostic_artifacts
                WHERE deployment_id = %s AND id = %s AND expires_at > %s
                """,
                (deployment_id, artifact_id, datetime.now(UTC)),
            ).fetchone()
        if row is None:
            raise DeploymentDiagnosticNotFoundError
        return _diagnostic(row, include_lines=True)

    def purge_expired_diagnostics(self, limit: int = 100) -> int:
        if limit < 1 or limit > 1000:
            raise ValueError("diagnostic purge limit must be between 1 and 1000")
        with self._database.connection() as connection:
            rows = connection.execute(
                """
                DELETE FROM deployment_diagnostic_artifacts
                WHERE id IN (
                    SELECT id FROM deployment_diagnostic_artifacts
                    WHERE expires_at <= %s
                    ORDER BY expires_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                RETURNING id
                """,
                (datetime.now(UTC), limit),
            ).fetchall()
        return len(rows)

    @staticmethod
    def _insert_diagnostic_artifacts(
        connection,
        *,
        deployment_id: UUID,
        event_id: int,
        failure_stage: str,
        failure_code: str,
        artifacts: Sequence[DiagnosticArtifactDraft],
        retention: timedelta,
    ) -> None:
        for artifact in artifacts:
            payload = [
                {
                    "timestamp": line.timestamp,
                    "stream": line.stream.value,
                    "message": line.message,
                }
                for line in artifact.lines
            ]
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) > DIAGNOSTIC_ARTIFACT_MAX_BYTES:
                raise ValueError("diagnostic artifact exceeds the byte limit")
            connection.execute(
                """
                INSERT INTO deployment_diagnostic_artifacts (
                    id, deployment_id, event_id, kind, failure_stage, failure_code,
                    capture_status, capture_code, operation, service_name, return_code,
                    container_status, container_exit_code, line_count, byte_count,
                    truncated, payload, captured_at, expires_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s::jsonb, %s, %s
                )
                ON CONFLICT DO NOTHING
                """,
                (
                    uuid4(),
                    deployment_id,
                    event_id,
                    artifact.kind.value,
                    failure_stage,
                    failure_code,
                    artifact.capture_status.value,
                    artifact.capture_code,
                    artifact.operation,
                    artifact.service_name,
                    artifact.return_code,
                    artifact.container_status,
                    artifact.container_exit_code,
                    len(artifact.lines),
                    len(encoded),
                    artifact.truncated,
                    encoded.decode("utf-8"),
                    artifact.captured_at,
                    artifact.captured_at + retention,
                ),
            )

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
    ) -> int:
        row = connection.execute(
            """
            INSERT INTO deployment_events (
                deployment_id, stage, code, message, created_at
            ) VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (deployment_id, stage, code, message, created_at),
        ).fetchone()
        connection.execute(
            "SELECT pg_notify(%s, %s)",
            (
                DEPLOYMENT_EVENT_CHANNEL,
                json.dumps(
                    {"deploymentId": str(deployment_id), "eventId": row["id"]},
                    separators=(",", ":"),
                ),
            ),
        )
        return row["id"]


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


def _diagnostic(row: dict, *, include_lines: bool) -> DeploymentDiagnosticArtifact:
    lines = None
    if include_lines:
        lines = tuple(
            DiagnosticLine(
                timestamp=item.get("timestamp"),
                stream=DiagnosticStream(item["stream"]),
                message=item["message"],
            )
            for item in row["payload"]
        )
    return DeploymentDiagnosticArtifact(
        id=row["id"],
        deployment_id=row["deployment_id"],
        event_id=row["event_id"],
        kind=DiagnosticArtifactKind(row["kind"]),
        failure_stage=row["failure_stage"],
        failure_code=row["failure_code"],
        capture_status=DiagnosticCaptureStatus(row["capture_status"]),
        capture_code=row["capture_code"],
        operation=row["operation"],
        service_name=row["service_name"],
        return_code=row["return_code"],
        container_status=row["container_status"],
        container_exit_code=row["container_exit_code"],
        line_count=row["line_count"],
        byte_count=row["byte_count"],
        truncated=row["truncated"],
        captured_at=row["captured_at"],
        expires_at=row["expires_at"],
        lines=lines,
    )


def _diagnostic_event_summary(
    artifacts: Sequence[DiagnosticArtifactDraft],
) -> tuple[str, str]:
    captured = sum(item.capture_status is DiagnosticCaptureStatus.CAPTURED for item in artifacts)
    if captured == len(artifacts) and artifacts:
        event_code = "DIAGNOSTIC_LOG_CAPTURED"
    elif captured:
        event_code = "DIAGNOSTIC_LOG_PARTIAL"
    else:
        event_code = "DIAGNOSTIC_LOG_UNAVAILABLE"
    return event_code, f"Stored {captured} of {len(artifacts)} bounded diagnostic artifacts"
