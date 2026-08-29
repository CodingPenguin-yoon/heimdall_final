from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from psycopg.errors import UniqueViolation

from heimdall.database import Database
from heimdall.public_routes.models import (
    PublicRoute,
    PublicRouteClaimLostError,
    PublicRouteConflictError,
    PublicRouteDesiredState,
    PublicRouteJobClaim,
    PublicRouteNotFoundError,
    PublicRouteStatus,
)

_HOSTNAME_CLAIM_LOCK = "heimdall-public-hostname-claims"


class PublicRouteRepository(Protocol):
    def get(self, project_id: UUID) -> PublicRoute: ...

    def set_enabled(self, project_id: UUID, subdomain: str, hostname: str) -> PublicRoute: ...

    def disable(self, project_id: UUID) -> PublicRoute: ...

    def list_applied(self) -> Sequence[PublicRoute]: ...

    def wake_pending(self, project_id: UUID) -> None: ...

    def claim_next(
        self, worker_id: str, lease_duration: timedelta
    ) -> PublicRouteJobClaim | None: ...

    def renew(self, claim: PublicRouteJobClaim, lease_duration: timedelta) -> datetime: ...

    def complete(self, claim: PublicRouteJobClaim) -> PublicRoute: ...

    def defer_not_ready(
        self, claim: PublicRouteJobClaim, available_at: datetime, code: str
    ) -> PublicRoute: ...

    def retry(
        self, claim: PublicRouteJobClaim, available_at: datetime, code: str
    ) -> PublicRoute: ...

    def fail(self, claim: PublicRouteJobClaim, code: str, *, uncertain: bool) -> PublicRoute: ...


class PostgresPublicRouteRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def get(self, project_id: UUID) -> PublicRoute:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM project_public_routes WHERE project_id = %s",
                (project_id,),
            ).fetchone()
        if row is None:
            raise PublicRouteNotFoundError
        return _route(row)

    def set_enabled(self, project_id: UUID, subdomain: str, hostname: str) -> PublicRoute:
        now = datetime.now(UTC)
        try:
            with self._database.connection() as connection:
                self._lock_hostname_claims(connection)
                current = connection.execute(
                    "SELECT * FROM project_public_routes WHERE project_id = %s FOR UPDATE",
                    (project_id,),
                ).fetchone()
                self._assert_hostname_available(connection, project_id, hostname)
                if current is None:
                    row = connection.execute(
                        """
                        INSERT INTO project_public_routes (
                            project_id, subdomain, hostname, desired_state, status,
                            desired_revision, created_at, updated_at
                        ) VALUES (%s, %s, %s, 'ENABLED', 'PENDING', 1, %s, %s)
                        RETURNING *
                        """,
                        (project_id, subdomain, hostname, now, now),
                    ).fetchone()
                    self._upsert_job(connection, project_id, 1, now)
                    return _route(row)

                unchanged = (
                    current["desired_state"] == PublicRouteDesiredState.ENABLED.value
                    and current["hostname"] == hostname
                )
                if unchanged and current["status"] in {
                    PublicRouteStatus.PENDING.value,
                    PublicRouteStatus.APPLYING.value,
                    PublicRouteStatus.ACTIVE.value,
                }:
                    return _route(current)
                revision = (
                    current["desired_revision"] if unchanged else current["desired_revision"] + 1
                )
                row = connection.execute(
                    """
                    UPDATE project_public_routes
                    SET subdomain = %s,
                        hostname = %s,
                        desired_state = 'ENABLED',
                        status = 'PENDING',
                        desired_revision = %s,
                        last_error_code = NULL,
                        updated_at = %s
                    WHERE project_id = %s
                    RETURNING *
                    """,
                    (subdomain, hostname, revision, now, project_id),
                ).fetchone()
                self._upsert_job(connection, project_id, revision, now)
        except UniqueViolation as error:
            raise PublicRouteConflictError from error
        return _route(row)

    def disable(self, project_id: UUID) -> PublicRoute:
        now = datetime.now(UTC)
        with self._database.connection() as connection:
            self._lock_hostname_claims(connection)
            current = connection.execute(
                "SELECT * FROM project_public_routes WHERE project_id = %s FOR UPDATE",
                (project_id,),
            ).fetchone()
            if current is None:
                raise PublicRouteNotFoundError
            unchanged = current["desired_state"] == PublicRouteDesiredState.DISABLED.value
            if unchanged and current["status"] in {
                PublicRouteStatus.PENDING.value,
                PublicRouteStatus.APPLYING.value,
                PublicRouteStatus.INACTIVE.value,
            }:
                return _route(current)
            revision = current["desired_revision"] if unchanged else current["desired_revision"] + 1
            row = connection.execute(
                """
                UPDATE project_public_routes
                SET desired_state = 'DISABLED',
                    status = 'PENDING',
                    desired_revision = %s,
                    last_error_code = NULL,
                    updated_at = %s
                WHERE project_id = %s
                RETURNING *
                """,
                (revision, now, project_id),
            ).fetchone()
            self._upsert_job(connection, project_id, revision, now)
        return _route(row)

    def list_applied(self) -> Sequence[PublicRoute]:
        with self._database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM project_public_routes
                WHERE applied_hostname IS NOT NULL
                ORDER BY applied_hostname, project_id
                """
            ).fetchall()
        return [_route(row) for row in rows]

    def wake_pending(self, project_id: UUID) -> None:
        with self._database.connection() as connection:
            connection.execute(
                """
                UPDATE public_route_jobs AS job
                SET available_at = LEAST(job.available_at, clock_timestamp()),
                    updated_at = clock_timestamp()
                WHERE job.project_id = %s
                  AND job.state = 'PENDING'
                  AND job.last_error_code = 'GATEWAY_START_FAILED'
                  AND EXISTS (
                      SELECT 1 FROM project_public_routes AS route
                      WHERE route.project_id = job.project_id
                        AND route.desired_state = 'ENABLED'
                        AND route.status = 'PENDING'
                        AND route.desired_revision = job.desired_revision
                        AND route.last_error_code = 'GATEWAY_START_FAILED'
                  )
                """,
                (project_id,),
            )

    def claim_next(
        self,
        worker_id: str,
        lease_duration: timedelta,
    ) -> PublicRouteJobClaim | None:
        token = uuid4()
        with self._database.connection() as connection:
            self._lock_hostname_claims(connection)
            job = connection.execute(
                """
                WITH candidate AS (
                    SELECT job.project_id
                    FROM public_route_jobs AS job
                    JOIN project_public_routes AS route
                      ON route.project_id = job.project_id
                     AND route.desired_revision = job.desired_revision
                    WHERE (
                        (job.state = 'PENDING' AND job.available_at <= clock_timestamp())
                        OR
                        (job.state = 'CLAIMED' AND job.lease_expires_at <= clock_timestamp())
                    )
                    ORDER BY job.available_at, job.created_at, job.project_id
                    FOR UPDATE OF job SKIP LOCKED
                    LIMIT 1
                )
                UPDATE public_route_jobs AS job
                SET state = 'CLAIMED',
                    attempts = job.attempts + 1,
                    lease_owner = %s,
                    lease_expires_at = clock_timestamp() + %s,
                    claim_token = %s,
                    last_error_code = NULL,
                    updated_at = clock_timestamp(),
                    completed_at = NULL
                FROM candidate
                WHERE job.project_id = candidate.project_id
                RETURNING job.*
                """,
                (worker_id, lease_duration, token),
            ).fetchone()
            if job is None:
                return None
            route = connection.execute(
                """
                UPDATE project_public_routes AS route
                SET status = 'APPLYING', last_error_code = NULL,
                    updated_at = clock_timestamp()
                WHERE route.project_id = %s AND route.desired_revision = %s
                  AND EXISTS (
                      SELECT 1 FROM public_route_jobs AS job
                      WHERE job.project_id = route.project_id
                        AND job.desired_revision = route.desired_revision
                        AND job.state = 'CLAIMED'
                        AND job.lease_owner = %s
                        AND job.claim_token = %s
                        AND job.lease_expires_at > clock_timestamp()
                  )
                RETURNING *
                """,
                (
                    job["project_id"],
                    job["desired_revision"],
                    worker_id,
                    token,
                ),
            ).fetchone()
            if route is None:
                raise PublicRouteClaimLostError
        return PublicRouteJobClaim(
            route=_route(route),
            token=token,
            worker_id=worker_id,
            desired_revision=job["desired_revision"],
            attempts=job["attempts"],
            lease_expires_at=job["lease_expires_at"],
        )

    def renew(self, claim: PublicRouteJobClaim, lease_duration: timedelta) -> datetime:
        with self._database.connection() as connection:
            row = connection.execute(
                """
                UPDATE public_route_jobs AS job
                SET lease_expires_at = clock_timestamp() + %s,
                    updated_at = clock_timestamp()
                WHERE job.project_id = %s
                  AND job.desired_revision = %s
                  AND job.state = 'CLAIMED'
                  AND job.lease_owner = %s
                  AND job.claim_token = %s
                  AND job.lease_expires_at > clock_timestamp()
                  AND EXISTS (
                      SELECT 1 FROM project_public_routes AS route
                      WHERE route.project_id = job.project_id
                        AND route.desired_revision = job.desired_revision
                  )
                RETURNING job.lease_expires_at
                """,
                (
                    lease_duration,
                    claim.route.project_id,
                    claim.desired_revision,
                    claim.worker_id,
                    claim.token,
                ),
            ).fetchone()
        if row is None:
            raise PublicRouteClaimLostError
        return row["lease_expires_at"]

    def complete(self, claim: PublicRouteJobClaim) -> PublicRoute:
        with self._database.connection() as connection:
            self._lock_hostname_claims(connection)
            self._lock_claim(connection, claim)
            current = connection.execute(
                "SELECT * FROM project_public_routes WHERE project_id = %s FOR UPDATE",
                (claim.route.project_id,),
            ).fetchone()
            if current["desired_state"] == PublicRouteDesiredState.ENABLED.value:
                status = PublicRouteStatus.ACTIVE.value
                applied_hostname = current["hostname"]
            else:
                status = PublicRouteStatus.INACTIVE.value
                applied_hostname = None
            row = connection.execute(
                """
                UPDATE project_public_routes AS route
                SET status = %s,
                    applied_revision = desired_revision,
                    applied_hostname = %s,
                    last_error_code = NULL,
                    updated_at = clock_timestamp()
                WHERE route.project_id = %s AND route.desired_revision = %s
                  AND EXISTS (
                      SELECT 1 FROM public_route_jobs AS job
                      WHERE job.project_id = route.project_id
                        AND job.desired_revision = route.desired_revision
                        AND job.state = 'CLAIMED'
                        AND job.lease_owner = %s
                        AND job.claim_token = %s
                        AND job.lease_expires_at > clock_timestamp()
                  )
                RETURNING *
                """,
                (
                    status,
                    applied_hostname,
                    claim.route.project_id,
                    claim.desired_revision,
                    claim.worker_id,
                    claim.token,
                ),
            ).fetchone()
            if row is None:
                raise PublicRouteClaimLostError
            self._finish_job(connection, claim, "SUCCEEDED", None)
        return _route(row)

    def defer_not_ready(
        self,
        claim: PublicRouteJobClaim,
        available_at: datetime,
        code: str,
    ) -> PublicRoute:
        return self._reschedule(claim, available_at, code)

    def retry(
        self,
        claim: PublicRouteJobClaim,
        available_at: datetime,
        code: str,
    ) -> PublicRoute:
        return self._reschedule(claim, available_at, code)

    def fail(
        self,
        claim: PublicRouteJobClaim,
        code: str,
        *,
        uncertain: bool,
    ) -> PublicRoute:
        status = PublicRouteStatus.UNCERTAIN if uncertain else PublicRouteStatus.FAILED
        with self._database.connection() as connection:
            self._lock_hostname_claims(connection)
            self._lock_claim(connection, claim)
            row = connection.execute(
                """
                UPDATE project_public_routes AS route
                SET status = %s, last_error_code = %s,
                    updated_at = clock_timestamp()
                WHERE route.project_id = %s AND route.desired_revision = %s
                  AND EXISTS (
                      SELECT 1 FROM public_route_jobs AS job
                      WHERE job.project_id = route.project_id
                        AND job.desired_revision = route.desired_revision
                        AND job.state = 'CLAIMED'
                        AND job.lease_owner = %s
                        AND job.claim_token = %s
                        AND job.lease_expires_at > clock_timestamp()
                  )
                RETURNING *
                """,
                (
                    status.value,
                    code,
                    claim.route.project_id,
                    claim.desired_revision,
                    claim.worker_id,
                    claim.token,
                ),
            ).fetchone()
            if row is None:
                raise PublicRouteClaimLostError
            self._finish_job(connection, claim, "FAILED", code)
        return _route(row)

    def _reschedule(
        self,
        claim: PublicRouteJobClaim,
        available_at: datetime,
        code: str,
    ) -> PublicRoute:
        with self._database.connection() as connection:
            self._lock_hostname_claims(connection)
            self._lock_claim(connection, claim)
            row = connection.execute(
                """
                UPDATE project_public_routes AS route
                SET status = 'PENDING', last_error_code = %s,
                    updated_at = clock_timestamp()
                WHERE route.project_id = %s AND route.desired_revision = %s
                  AND EXISTS (
                      SELECT 1 FROM public_route_jobs AS job
                      WHERE job.project_id = route.project_id
                        AND job.desired_revision = route.desired_revision
                        AND job.state = 'CLAIMED'
                        AND job.lease_owner = %s
                        AND job.claim_token = %s
                        AND job.lease_expires_at > clock_timestamp()
                  )
                RETURNING *
                """,
                (
                    code,
                    claim.route.project_id,
                    claim.desired_revision,
                    claim.worker_id,
                    claim.token,
                ),
            ).fetchone()
            if row is None:
                raise PublicRouteClaimLostError
            updated = connection.execute(
                """
                UPDATE public_route_jobs
                SET state = 'PENDING', available_at = %s,
                    lease_owner = NULL, lease_expires_at = NULL, claim_token = NULL,
                    last_error_code = %s, updated_at = clock_timestamp(),
                    completed_at = NULL
                WHERE project_id = %s
                  AND desired_revision = %s
                  AND state = 'CLAIMED'
                  AND lease_owner = %s
                  AND claim_token = %s
                  AND lease_expires_at > clock_timestamp()
                """,
                (
                    available_at,
                    code,
                    claim.route.project_id,
                    claim.desired_revision,
                    claim.worker_id,
                    claim.token,
                ),
            )
            if updated.rowcount != 1:
                raise PublicRouteClaimLostError
        return _route(row)

    @staticmethod
    def _lock_hostname_claims(connection) -> None:
        connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_HOSTNAME_CLAIM_LOCK,))

    @staticmethod
    def _assert_hostname_available(connection, project_id: UUID, hostname: str) -> None:
        conflict = connection.execute(
            """
            SELECT project_id FROM project_public_routes
            WHERE project_id <> %s
              AND (hostname = %s OR applied_hostname = %s)
            LIMIT 1
            """,
            (project_id, hostname, hostname),
        ).fetchone()
        if conflict is not None:
            raise PublicRouteConflictError

    @staticmethod
    def _upsert_job(connection, project_id: UUID, revision: int, now: datetime) -> None:
        connection.execute(
            """
            INSERT INTO public_route_jobs (
                project_id, desired_revision, state, available_at, created_at, updated_at
            ) VALUES (%s, %s, 'PENDING', %s, %s, %s)
            ON CONFLICT (project_id) DO UPDATE
            SET desired_revision = EXCLUDED.desired_revision,
                state = 'PENDING',
                attempts = 0,
                available_at = EXCLUDED.available_at,
                lease_owner = NULL,
                lease_expires_at = NULL,
                claim_token = NULL,
                last_error_code = NULL,
                updated_at = EXCLUDED.updated_at,
                completed_at = NULL
            """,
            (project_id, revision, now, now, now),
        )

    @staticmethod
    def _lock_claim(connection, claim: PublicRouteJobClaim) -> None:
        row = connection.execute(
            """
            SELECT job.project_id
            FROM public_route_jobs AS job
            JOIN project_public_routes AS route
              ON route.project_id = job.project_id
             AND route.desired_revision = job.desired_revision
            WHERE job.project_id = %s
              AND job.desired_revision = %s
              AND job.state = 'CLAIMED'
              AND job.lease_owner = %s
              AND job.claim_token = %s
              AND job.lease_expires_at > clock_timestamp()
            FOR UPDATE OF job, route
            """,
            (
                claim.route.project_id,
                claim.desired_revision,
                claim.worker_id,
                claim.token,
            ),
        ).fetchone()
        if row is None:
            raise PublicRouteClaimLostError

    @staticmethod
    def _finish_job(
        connection,
        claim: PublicRouteJobClaim,
        state: str,
        code: str | None,
    ) -> None:
        updated = connection.execute(
            """
            UPDATE public_route_jobs
            SET state = %s,
                lease_owner = NULL,
                lease_expires_at = NULL,
                claim_token = NULL,
                last_error_code = %s,
                updated_at = clock_timestamp(),
                completed_at = clock_timestamp()
            WHERE project_id = %s
              AND desired_revision = %s
              AND state = 'CLAIMED'
              AND lease_owner = %s
              AND claim_token = %s
              AND lease_expires_at > clock_timestamp()
            """,
            (
                state,
                code,
                claim.route.project_id,
                claim.desired_revision,
                claim.worker_id,
                claim.token,
            ),
        )
        if updated.rowcount != 1:
            raise PublicRouteClaimLostError


def _route(row: dict) -> PublicRoute:
    return PublicRoute(
        project_id=row["project_id"],
        subdomain=row["subdomain"],
        hostname=row["hostname"],
        desired_state=PublicRouteDesiredState(row["desired_state"]),
        status=PublicRouteStatus(row["status"]),
        desired_revision=row["desired_revision"],
        applied_revision=row["applied_revision"],
        applied_hostname=row["applied_hostname"],
        last_error_code=row["last_error_code"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
