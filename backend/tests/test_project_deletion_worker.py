from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from heimdall.deployments.worker import RuntimeFailure
from heimdall.project_database.models import ProjectDatabaseProvisioningError
from heimdall.projects.deletion_worker import (
    ProjectDatabaseDeletionAdapter,
    ProjectDeletionWorker,
)
from heimdall.projects.models import (
    ProjectDeletionJob,
    ProjectDeletionJobClaim,
    ProjectDeletionPhase,
    ProjectDeletionState,
)
from heimdall.secrets.store import SecretStoreBusyError
from heimdall.worker import _run_workers_once


def deletion_job(*, attempts: int = 1) -> ProjectDeletionJob:
    now = datetime.now(UTC)
    return ProjectDeletionJob(
        project_id=uuid4(),
        state=ProjectDeletionState.CLAIMED,
        phase=ProjectDeletionPhase.REQUESTED,
        attempts=attempts,
        available_at=now,
        lease_owner="worker-one",
        lease_expires_at=now + timedelta(minutes=1),
        claim_token=uuid4(),
        last_error_code=None,
        last_error_retryable=None,
        delete_managed_database=True,
        created_at=now,
        updated_at=now,
    )


class MemoryProjects:
    def __init__(self, job: ProjectDeletionJob) -> None:
        self.job = job
        self.available = True
        self.operations_drained = True
        self.guard_allowed = True
        self.transitions: list[ProjectDeletionPhase] = []
        self.reschedules: list[str] = []
        self.deferrals = 0
        self.failures: list[tuple[str, bool]] = []
        self.finalized = False
        self.lose_claim_while_persisting = False

    def claim_next_deletion(self, worker_id: str, lease_duration: timedelta):
        if not self.available:
            return None
        self.available = False
        token = self.job.claim_token or uuid4()
        return ProjectDeletionJobClaim(
            job=self.job,
            token=token,
            worker_id=worker_id,
            lease_expires_at=datetime.now(UTC) + lease_duration,
        )

    def renew_deletion(self, claim, lease_duration):
        return datetime.now(UTC) + lease_duration

    def advance_deletion(self, claim, expected_phase, next_phase):
        assert self.job.phase is expected_phase
        self.job = replace(self.job, phase=next_phase)
        self.transitions.append(next_phase)
        return self.job

    def reschedule_deletion(self, claim, available_at, code):
        if self.lose_claim_while_persisting:
            from heimdall.projects.models import ProjectDeletionClaimLostError

            raise ProjectDeletionClaimLostError
        self.reschedules.append(code)
        self.job = replace(
            self.job,
            state=ProjectDeletionState.PENDING,
            available_at=available_at,
            lease_owner=None,
            lease_expires_at=None,
            claim_token=None,
        )

    def defer_deletion(self, claim, available_at):
        self.deferrals += 1
        self.job = replace(
            self.job,
            state=ProjectDeletionState.PENDING,
            attempts=max(self.job.attempts - 1, 0),
            available_at=available_at,
            lease_owner=None,
            lease_expires_at=None,
            claim_token=None,
        )

    def fail_deletion(self, claim, code, *, retryable):
        if self.lose_claim_while_persisting:
            from heimdall.projects.models import ProjectDeletionClaimLostError

            raise ProjectDeletionClaimLostError
        self.failures.append((code, retryable))
        self.job = replace(self.job, state=ProjectDeletionState.FAILED)

    def deletion_operations_drained(self, project_id: UUID) -> bool:
        return self.operations_drained

    def deletion_runtime_snapshot(self, project_id: UUID):
        return ("deployment-snapshot",), "runtime-snapshot"

    def deletion_mutation_allowed(self, claim, *, require_route_removed: bool) -> bool:
        return self.guard_allowed

    def finalize_deletion(self, claim) -> None:
        assert self.guard_allowed
        self.finalized = True

    def reclaim(self) -> None:
        self.available = True
        self.job = replace(
            self.job,
            state=ProjectDeletionState.CLAIMED,
            attempts=self.job.attempts + 1,
            lease_owner="worker-one",
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            claim_token=uuid4(),
        )


class MemoryRoutes:
    def __init__(self, operations: list[str]) -> None:
        self.operations = operations
        self.applied = False

    def disable_for_deletion(self, project_id: UUID) -> None:
        self.operations.append("route-disable")

    def deletion_is_applied(self, project_id: UUID) -> bool:
        return self.applied


class MemoryRuntime:
    def __init__(self, operations: list[str]) -> None:
        self.operations = operations

    def teardown(self, project_id, deployments, runtime, *, mutation_guard, heartbeat):
        assert deployments == ("deployment-snapshot",)
        assert runtime == "runtime-snapshot"
        assert heartbeat() is True
        assert mutation_guard() is True
        self.operations.append("runtime")

    def verify_absent(self, project_id, deployments, runtime, *, mutation_guard, heartbeat):
        assert mutation_guard() is True
        assert heartbeat() is True


class MemorySecrets:
    def __init__(self, operations: list[str]) -> None:
        self.operations = operations

    @contextmanager
    def project_operation_lock(self, project_id: UUID, *, blocking: bool = True):
        yield

    def delete_project_subtree(self, project_id: UUID) -> None:
        self.operations.append("secret")

    def project_subtree_absent(self, project_id: UUID) -> bool:
        return True


class Resource:
    def __init__(self) -> None:
        self.id = uuid4()


class MemoryDatabases:
    def __init__(self, operations: list[str]) -> None:
        self.operations = operations
        self.resource = Resource()

    def get_resource(self, project_id: UUID):
        return self.resource

    @contextmanager
    def try_operation_lock(self, resource_id: UUID):
        yield True

    def quiesce(self, resource) -> None:
        self.operations.append("database-quiesce")

    def drop_database(self, resource) -> None:
        self.operations.append("database-drop")

    def drop_role(self, resource) -> None:
        self.operations.append("role-drop")

    def verify_absent(self, resource) -> None:
        return None


def make_worker(
    projects: MemoryProjects,
    operations: list[str],
    routes: MemoryRoutes,
    *,
    secrets=None,
    databases=None,
):
    return ProjectDeletionWorker(
        projects,
        routes,
        MemoryRuntime(operations),
        secrets or MemorySecrets(operations),
        databases or MemoryDatabases(operations),
        worker_id="worker-one",
        lease_duration=timedelta(minutes=1),
        max_attempts=5,
        retry_base_delay=timedelta(milliseconds=1),
    )


def test_worker_retries_when_secret_lock_becomes_busy_before_cleanup() -> None:
    operations: list[str] = []
    job = replace(deletion_job(), phase=ProjectDeletionPhase.SECRET_CLEANUP)
    projects = MemoryProjects(job)
    routes = MemoryRoutes(operations)
    routes.applied = True

    class BusySecrets(MemorySecrets):
        @contextmanager
        def project_operation_lock(self, project_id: UUID, *, blocking: bool = True):
            raise SecretStoreBusyError("project secret operation is active")
            yield

    worker = make_worker(
        projects,
        operations,
        routes,
        secrets=BusySecrets(operations),
    )

    assert worker.run_once() is True

    assert operations == []
    assert projects.reschedules == ["PROJECT_SECRET_OPERATION_ACTIVE"]
    assert projects.failures == []


def test_worker_fails_closed_when_database_authorization_and_resource_disagree() -> None:
    operations: list[str] = []
    job = replace(
        deletion_job(),
        phase=ProjectDeletionPhase.DATABASE_QUIESCING,
        delete_managed_database=False,
    )
    projects = MemoryProjects(job)
    routes = MemoryRoutes(operations)
    routes.applied = True
    worker = make_worker(projects, operations, routes)

    assert worker.run_once() is True

    assert operations == []
    assert projects.failures == [("PROJECT_DATABASE_AUTHORIZATION_MISMATCH", False)]


def test_worker_preserves_database_failure_across_operation_lock_exit() -> None:
    operations: list[str] = []
    job = replace(deletion_job(), phase=ProjectDeletionPhase.DATABASE_QUIESCING)
    projects = MemoryProjects(job)
    routes = MemoryRoutes(operations)
    routes.applied = True

    class ConflictingDatabases(MemoryDatabases):
        def quiesce(self, resource) -> None:
            raise RuntimeFailure(
                "DELETION",
                "PROJECT_DATABASE_OWNERSHIP_CONFLICT",
                retryable=False,
                cleanup_candidate=False,
            )

    worker = make_worker(
        projects,
        operations,
        routes,
        databases=ConflictingDatabases(operations),
    )

    assert worker.run_once() is True
    assert projects.failures == [("PROJECT_DATABASE_OWNERSHIP_CONFLICT", False)]


def test_worker_reobserves_all_external_resources_before_metadata_finalize() -> None:
    operations: list[str] = []
    job = replace(deletion_job(), phase=ProjectDeletionPhase.METADATA_DELETE)
    projects = MemoryProjects(job)
    routes = MemoryRoutes(operations)
    routes.applied = True

    class RecreatedSecrets(MemorySecrets):
        def project_subtree_absent(self, project_id: UUID) -> bool:
            return False

    worker = make_worker(
        projects,
        operations,
        routes,
        secrets=RecreatedSecrets(operations),
    )

    assert worker.run_once() is True

    assert projects.finalized is False
    assert projects.failures == [("PROJECT_SECRET_RESOURCES_REAPPEARED", False)]


def test_worker_suppresses_claim_loss_while_persisting_a_runtime_failure() -> None:
    operations: list[str] = []
    projects = MemoryProjects(replace(deletion_job(), phase=ProjectDeletionPhase.ROUTE_DISABLING))
    projects.lose_claim_while_persisting = True

    class FailingRoutes(MemoryRoutes):
        def disable_for_deletion(self, project_id: UUID) -> None:
            raise RuntimeFailure(
                "DELETION",
                "PROJECT_ROUTE_OBSERVATION_FAILED",
                retryable=True,
                cleanup_candidate=False,
            )

    routes = FailingRoutes(operations)
    worker = make_worker(projects, operations, routes)

    assert worker.run_once() is True


def test_worker_does_not_remove_runtime_until_edge_route_removal_is_applied() -> None:
    operations: list[str] = []
    projects = MemoryProjects(deletion_job())
    routes = MemoryRoutes(operations)
    worker = make_worker(projects, operations, routes)

    assert worker.run_once() is True

    assert operations == ["route-disable"]
    assert projects.job.phase is ProjectDeletionPhase.ROUTE_DISABLING
    assert projects.deferrals == 1
    assert projects.finalized is False


def test_normal_operation_wait_does_not_consume_failure_attempt_budget() -> None:
    operations: list[str] = []
    projects = MemoryProjects(deletion_job())
    projects.operations_drained = False
    routes = MemoryRoutes(operations)
    worker = make_worker(projects, operations, routes)

    for _ in range(6):
        assert worker.run_once() is True
        projects.reclaim()

    assert projects.job.attempts == 1
    assert projects.failures == []


def test_worker_resumes_by_phase_and_finalizes_only_after_ordered_external_cleanup() -> None:
    operations: list[str] = []
    projects = MemoryProjects(deletion_job())
    routes = MemoryRoutes(operations)
    worker = make_worker(projects, operations, routes)
    worker.run_once()
    routes.applied = True
    projects.reclaim()

    assert worker.run_once() is True

    assert operations == [
        "route-disable",
        "route-disable",
        "runtime",
        "database-quiesce",
        "database-drop",
        "role-drop",
        "secret",
    ]
    assert projects.finalized is True


def test_worker_fails_at_attempt_cap_without_repeating_external_mutation() -> None:
    operations: list[str] = []
    projects = MemoryProjects(deletion_job(attempts=2))
    routes = MemoryRoutes(operations)
    worker = ProjectDeletionWorker(
        projects,
        routes,
        MemoryRuntime(operations),
        MemorySecrets(operations),
        MemoryDatabases(operations),
        worker_id="worker-one",
        lease_duration=timedelta(minutes=1),
        max_attempts=1,
        retry_base_delay=timedelta(milliseconds=1),
    )

    assert worker.run_once() is True

    assert operations == []
    assert projects.failures == [("PROJECT_DELETION_ATTEMPTS_EXHAUSTED", True)]


def test_worker_suppresses_claim_loss_at_attempt_cap() -> None:
    operations: list[str] = []
    projects = MemoryProjects(deletion_job(attempts=2))
    projects.lose_claim_while_persisting = True
    routes = MemoryRoutes(operations)
    worker = ProjectDeletionWorker(
        projects,
        routes,
        MemoryRuntime(operations),
        MemorySecrets(operations),
        MemoryDatabases(operations),
        worker_id="worker-one",
        lease_duration=timedelta(minutes=1),
        max_attempts=1,
        retry_base_delay=timedelta(milliseconds=1),
    )

    assert worker.run_once() is True


def test_database_deletion_adapter_maps_nonblocking_busy_lock_to_not_acquired() -> None:
    class Repository:
        def get_for_project(self, project_id):
            return "resource"

    class Provisioner:
        @contextmanager
        def operation_lock(self, resource_id, *, blocking=True):
            assert blocking is False
            raise ProjectDatabaseProvisioningError("DELETE", "OPERATION_LOCK_BUSY")
            yield

    adapter = ProjectDatabaseDeletionAdapter(Repository(), Provisioner())

    assert adapter.get_resource(uuid4()) == "resource"
    with adapter.try_operation_lock(uuid4()) as acquired:
        assert acquired is False


def test_database_deletion_adapter_fails_closed_when_admin_is_unavailable() -> None:
    class Repository:
        def get_for_project(self, project_id):
            return "resource"

    adapter = ProjectDatabaseDeletionAdapter(Repository(), None)
    resource = Resource()
    resource.database_name = "database"
    resource.role_name = "role"

    with pytest.raises(RuntimeFailure) as raised:
        adapter.quiesce(resource)

    assert raised.value.code == "PROJECT_DATABASE_DELETION_UNAVAILABLE"


def test_database_deletion_adapter_does_not_treat_unlock_failure_as_busy() -> None:
    class Repository:
        def get_for_project(self, project_id):
            return "resource"

    class Provisioner:
        @contextmanager
        def operation_lock(self, resource_id, *, blocking=True):
            yield
            raise ProjectDatabaseProvisioningError("DELETE", "OPERATION_LOCK_FAILED")

    adapter = ProjectDatabaseDeletionAdapter(Repository(), Provisioner())

    with (
        pytest.raises(RuntimeFailure) as raised,
        adapter.try_operation_lock(uuid4()) as acquired,
    ):
        assert acquired is True

    assert raised.value.code == "PROJECT_DATABASE_OPERATION_LOCK_FAILED"


def test_worker_loop_prioritizes_deletion_before_deployment_and_reconciliation() -> None:
    calls: list[str] = []

    class Worker:
        def __init__(self, name: str, result: bool) -> None:
            self.name = name
            self.result = result

        def run_once(self) -> bool:
            calls.append(self.name)
            return self.result

    assert (
        _run_workers_once(
            Worker("deletion", True),
            Worker("deployment", True),
            Worker("reconciliation", True),
        )
        is True
    )
    assert calls == ["deletion"]
