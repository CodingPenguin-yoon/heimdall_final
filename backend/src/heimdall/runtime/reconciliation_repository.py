from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from heimdall.database import Database
from heimdall.runtime.reconciliation import (
    ReconciliationAction,
    ReconciliationClaimLostError,
    ReconciliationInProgressError,
    ReconciliationProjectDeletingError,
    ReconciliationRequester,
    ReconciliationResult,
    ReconciliationState,
    RuntimeReconciliation,
    RuntimeReconciliationClaim,
)


class PostgresRuntimeReconciliationRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def get(self, deployment_id: UUID) -> RuntimeReconciliation | None:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_reconciliations WHERE deployment_id = %s",
                (deployment_id,),
            ).fetchone()
        return _reconciliation(row) if row is not None else None

    def request(
        self,
        deployment_id: UUID,
        action: ReconciliationAction,
        requested_by: ReconciliationRequester,
    ) -> RuntimeReconciliation:
        now = datetime.now(UTC)
        with self._database.connection() as connection:
            project = connection.execute(
                """
                SELECT project.status
                FROM projects AS project
                JOIN deployments AS deployment ON deployment.project_id = project.id
                WHERE deployment.id = %s
                FOR UPDATE OF project
                """,
                (deployment_id,),
            ).fetchone()
            if project is not None and project["status"] == "DELETING":
                raise ReconciliationProjectDeletingError
            row = connection.execute(
                """
                INSERT INTO runtime_reconciliations (
                    deployment_id, action, requested_by, state, available_at,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, 'PENDING', %s, %s, %s)
                ON CONFLICT (deployment_id) DO UPDATE
                SET action = EXCLUDED.action,
                    requested_by = EXCLUDED.requested_by,
                    state = 'PENDING',
                    result = NULL,
                    result_code = NULL,
                    attempts = 0,
                    available_at = EXCLUDED.available_at,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    claim_token = NULL,
                    updated_at = EXCLUDED.updated_at,
                    completed_at = NULL
                WHERE runtime_reconciliations.state <> 'CLAIMED'
                RETURNING *
                """,
                (deployment_id, action.value, requested_by.value, now, now, now),
            ).fetchone()
        if row is None:
            raise ReconciliationInProgressError
        return _reconciliation(row)

    def claim_next(
        self,
        worker_id: str,
        lease_duration: timedelta,
    ) -> RuntimeReconciliationClaim | None:
        now = datetime.now(UTC)
        lease_expires_at = now + lease_duration
        token = uuid4()
        with self._database.connection() as connection:
            row = connection.execute(
                """
                WITH candidate AS (
                    SELECT deployment_id
                    FROM runtime_reconciliations
                    WHERE (
                        (state = 'PENDING' AND available_at <= %s)
                        OR
                        (state = 'CLAIMED' AND lease_expires_at <= %s)
                    )
                    ORDER BY available_at, created_at, deployment_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE runtime_reconciliations AS reconciliation
                SET state = 'CLAIMED',
                    attempts = reconciliation.attempts + 1,
                    lease_owner = %s,
                    lease_expires_at = %s,
                    claim_token = %s,
                    updated_at = %s,
                    completed_at = NULL
                FROM candidate
                WHERE reconciliation.deployment_id = candidate.deployment_id
                RETURNING reconciliation.*
                """,
                (now, now, worker_id, lease_expires_at, token, now),
            ).fetchone()
        if row is None:
            return None
        return RuntimeReconciliationClaim(
            reconciliation=_reconciliation(row),
            token=token,
            worker_id=worker_id,
            lease_expires_at=lease_expires_at,
        )

    def schedule_automatic(self, candidates: Sequence[tuple[UUID, datetime]]) -> None:
        if not candidates:
            return
        now = datetime.now(UTC)
        with self._database.connection() as connection:
            for deployment_id, available_at in candidates:
                project = connection.execute(
                    """
                    SELECT project.status
                    FROM projects AS project
                    JOIN deployments AS deployment ON deployment.project_id = project.id
                    WHERE deployment.id = %s
                    FOR UPDATE OF project
                    """,
                    (deployment_id,),
                ).fetchone()
                if project is None or project["status"] == "DELETING":
                    continue
                connection.execute(
                    """
                    INSERT INTO runtime_reconciliations (
                        deployment_id, action, requested_by, state, available_at,
                        created_at, updated_at
                    ) VALUES (%s, 'RECONCILE', 'SYSTEM', 'PENDING', %s, %s, %s)
                    ON CONFLICT (deployment_id) DO NOTHING
                    """,
                    (deployment_id, available_at, now, now),
                )

    def renew(self, claim: RuntimeReconciliationClaim, lease_duration: timedelta) -> datetime:
        now = datetime.now(UTC)
        lease_expires_at = now + lease_duration
        with self._database.connection() as connection:
            row = connection.execute(
                """
                UPDATE runtime_reconciliations
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
                    claim.reconciliation.deployment_id,
                    claim.worker_id,
                    claim.token,
                    now,
                ),
            ).fetchone()
        if row is None:
            raise ReconciliationClaimLostError
        return row["lease_expires_at"]

    def resolve(
        self,
        claim: RuntimeReconciliationClaim,
        result: ReconciliationResult,
        result_code: str,
    ) -> RuntimeReconciliation:
        if result is ReconciliationResult.UNCERTAIN:
            raise ValueError("UNCERTAIN reconciliation must be blocked")
        return self._finish(claim, ReconciliationState.RESOLVED, result, result_code)

    def block(self, claim: RuntimeReconciliationClaim, result_code: str) -> RuntimeReconciliation:
        return self._finish(
            claim,
            ReconciliationState.BLOCKED,
            ReconciliationResult.UNCERTAIN,
            result_code,
        )

    def retry(
        self,
        claim: RuntimeReconciliationClaim,
        available_at: datetime,
        result_code: str,
    ) -> RuntimeReconciliation:
        now = datetime.now(UTC)
        with self._database.connection() as connection:
            self._lock_claim(connection, claim, now)
            row = connection.execute(
                """
                UPDATE runtime_reconciliations
                SET state = 'PENDING', result = NULL, result_code = %s,
                    available_at = %s, lease_owner = NULL,
                    lease_expires_at = NULL, claim_token = NULL,
                    updated_at = %s, completed_at = NULL
                WHERE deployment_id = %s
                RETURNING *
                """,
                (result_code, available_at, now, claim.reconciliation.deployment_id),
            ).fetchone()
        return _reconciliation(row)

    def _finish(
        self,
        claim: RuntimeReconciliationClaim,
        state: ReconciliationState,
        result: ReconciliationResult,
        result_code: str,
    ) -> RuntimeReconciliation:
        now = datetime.now(UTC)
        with self._database.connection() as connection:
            self._lock_claim(connection, claim, now)
            row = connection.execute(
                """
                UPDATE runtime_reconciliations
                SET state = %s, result = %s, result_code = %s,
                    lease_owner = NULL, lease_expires_at = NULL, claim_token = NULL,
                    updated_at = %s, completed_at = %s
                WHERE deployment_id = %s
                RETURNING *
                """,
                (
                    state.value,
                    result.value,
                    result_code,
                    now,
                    now,
                    claim.reconciliation.deployment_id,
                ),
            ).fetchone()
        return _reconciliation(row)

    @staticmethod
    def _lock_claim(connection, claim: RuntimeReconciliationClaim, now: datetime) -> None:
        row = connection.execute(
            """
            SELECT deployment_id
            FROM runtime_reconciliations
            WHERE deployment_id = %s
              AND state = 'CLAIMED'
              AND lease_owner = %s
              AND claim_token = %s
              AND lease_expires_at > %s
            FOR UPDATE
            """,
            (
                claim.reconciliation.deployment_id,
                claim.worker_id,
                claim.token,
                now,
            ),
        ).fetchone()
        if row is None:
            raise ReconciliationClaimLostError


def _reconciliation(row: dict) -> RuntimeReconciliation:
    return RuntimeReconciliation(
        deployment_id=row["deployment_id"],
        action=ReconciliationAction(row["action"]),
        requested_by=ReconciliationRequester(row["requested_by"]),
        state=ReconciliationState(row["state"]),
        result=ReconciliationResult(row["result"]) if row["result"] is not None else None,
        result_code=row["result_code"],
        attempts=row["attempts"],
        available_at=row["available_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
    )
