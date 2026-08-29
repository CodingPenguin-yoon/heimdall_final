from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from heimdall.projects.models import (
    ProjectDeletionJob,
    ProjectDeletionJobClaim,
    ProjectDeletionPhase,
    ProjectDeletionState,
)
from heimdall.projects.repository import PostgresProjectRepository
from heimdall.public_routes.repository import PostgresPublicRouteRepository


class Result:
    def __init__(self, row=None, rows=()) -> None:
        self.row = row
        self.rows = rows

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class RecordingConnection:
    def __init__(self, responses=()) -> None:
        self.responses = list(responses)
        self.statements: list[tuple[str, tuple]] = []

    def execute(self, statement: str, values=()):
        normalized = " ".join(statement.split())
        self.statements.append((normalized, tuple(values)))
        returns_row = normalized.startswith("SELECT") or "RETURNING" in normalized
        return Result(self.responses.pop(0) if returns_row and self.responses else None)


class RecordingDatabase:
    def __init__(self, responses=()) -> None:
        self.connection_value = RecordingConnection(responses)

    @contextmanager
    def connection(self):
        yield self.connection_value


def claim() -> ProjectDeletionJobClaim:
    now = datetime.now(UTC)
    project_id = uuid4()
    token = uuid4()
    job = ProjectDeletionJob(
        project_id=project_id,
        state=ProjectDeletionState.CLAIMED,
        phase=ProjectDeletionPhase.METADATA_DELETE,
        attempts=1,
        available_at=now,
        lease_owner="worker-one",
        lease_expires_at=now + timedelta(minutes=1),
        claim_token=token,
        last_error_code=None,
        last_error_retryable=None,
        delete_managed_database=False,
        created_at=now,
        updated_at=now,
    )
    return ProjectDeletionJobClaim(
        job=job,
        token=token,
        worker_id="worker-one",
        lease_expires_at=job.lease_expires_at,
    )


def test_operation_drain_snapshot_covers_deployment_route_and_reconciliation_work() -> None:
    database = RecordingDatabase([{"drained": False}])
    repository = PostgresProjectRepository(database)  # type: ignore[arg-type]
    project_id = uuid4()

    assert repository.deletion_operations_drained(project_id) is False

    query = database.connection_value.statements[0][0]
    assert "deployment_jobs" in query
    assert "public_route_jobs" in query
    assert "runtime_reconciliations" in query


def test_finalization_uses_live_claim_fence_and_explicit_child_delete_order() -> None:
    deletion_claim = claim()
    database = RecordingDatabase(
        [
            {"project_id": deletion_claim.job.project_id},
            {"id": deletion_claim.job.project_id},
        ]
    )
    repository = PostgresProjectRepository(database)  # type: ignore[arg-type]

    repository.finalize_deletion(deletion_claim)

    statements = [statement for statement, _ in database.connection_value.statements]
    assert "claim_token" in statements[0]
    assert "lease_expires_at > clock_timestamp()" in statements[0]
    delete_tables = [
        statement.split(" ")[2]
        for statement in statements[1:]
        if statement.startswith("DELETE FROM")
    ]
    assert delete_tables == [
        "project_runtimes",
        "public_route_jobs",
        "project_public_routes",
        "runtime_reconciliations",
        "deployments",
        "project_environment_secrets",
        "project_database_resources",
        "project_deletion_jobs",
        "projects",
    ]


def test_deletion_route_disable_is_separate_and_requires_a_deleting_project() -> None:
    database = RecordingDatabase([{"status": "DELETING"}, None])
    repository = PostgresPublicRouteRepository(database)  # type: ignore[arg-type]
    project_id = uuid4()

    assert repository.disable_for_deletion(project_id) is None

    statements = [statement for statement, _ in database.connection_value.statements]
    assert "FOR UPDATE" in statements[0]
    assert "project_public_routes" in statements[-1]


def test_deletion_route_observation_requires_inactive_without_applied_hostname() -> None:
    database = RecordingDatabase([{"applied": True}])
    repository = PostgresPublicRouteRepository(database)  # type: ignore[arg-type]

    assert repository.deletion_is_applied(uuid4()) is True

    statement = database.connection_value.statements[0][0]
    assert "status = 'INACTIVE'" in statement
    assert "applied_hostname IS NULL" in statement


def test_claim_and_lease_fences_use_database_clock() -> None:
    database = RecordingDatabase()
    repository = PostgresProjectRepository(database)  # type: ignore[arg-type]

    assert repository.claim_next_deletion("worker-one", timedelta(minutes=1)) is None

    statement, values = database.connection_value.statements[0]
    assert "available_at <= clock_timestamp()" in statement
    assert "lease_expires_at <= clock_timestamp()" in statement
    assert "clock_timestamp() +" in statement
    assert len(values) == 3


def test_explicit_retry_resets_attempt_budget() -> None:
    deletion_claim = claim()
    job = deletion_claim.job
    row = {
        "project_id": job.project_id,
        "state": "PENDING",
        "phase": "ROUTE_DISABLING",
        "attempts": 0,
        "available_at": job.available_at,
        "lease_owner": None,
        "lease_expires_at": None,
        "claim_token": None,
        "last_error_code": None,
        "last_error_retryable": None,
        "delete_managed_database": False,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }
    database = RecordingDatabase([{"status": "DELETING"}, None, row])
    repository = PostgresProjectRepository(database)  # type: ignore[arg-type]

    retried = repository.retry_deletion(
        job.project_id,
        confirmation=str(job.project_id),
        delete_managed_database=False,
        managed_database_confirmation=None,
    )

    assert retried.attempts == 0
    update = next(
        statement
        for statement, _ in database.connection_value.statements
        if statement.startswith("UPDATE project_deletion_jobs")
    )
    assert "attempts = 0" in update


def test_normal_deletion_wait_releases_claim_without_consuming_attempt() -> None:
    deletion_claim = claim()
    job = deletion_claim.job
    row = {
        "project_id": job.project_id,
        "state": "PENDING",
        "phase": job.phase.value,
        "attempts": 0,
        "available_at": job.available_at,
        "lease_owner": None,
        "lease_expires_at": None,
        "claim_token": None,
        "last_error_code": None,
        "last_error_retryable": None,
        "delete_managed_database": False,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }
    database = RecordingDatabase([{"project_id": job.project_id}, row])
    repository = PostgresProjectRepository(database)  # type: ignore[arg-type]

    deferred = repository.defer_deletion(deletion_claim, datetime.now(UTC) + timedelta(seconds=1))

    assert deferred.attempts == 0
    update = database.connection_value.statements[1][0]
    assert "attempts = GREATEST(attempts - 1, 0)" in update
