from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from heimdall.deployments.models import Deployment
from heimdall.deployments.worker import RuntimeFailure
from heimdall.project_database.models import ProjectDatabaseProvisioningError
from heimdall.projects.models import (
    ProjectDeletionClaimLostError,
    ProjectDeletionJob,
    ProjectDeletionJobClaim,
    ProjectDeletionPhase,
)
from heimdall.runtime.repository import ProjectRuntime
from heimdall.secrets.store import SecretStoreBusyError, SecretStoreError


class DeletionProjectRepository(Protocol):
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

    def reschedule_deletion(
        self, claim: ProjectDeletionJobClaim, available_at: datetime, code: str
    ) -> ProjectDeletionJob: ...

    def defer_deletion(
        self, claim: ProjectDeletionJobClaim, available_at: datetime
    ) -> ProjectDeletionJob: ...

    def fail_deletion(
        self, claim: ProjectDeletionJobClaim, code: str, *, retryable: bool
    ) -> ProjectDeletionJob: ...

    def deletion_operations_drained(self, project_id: UUID) -> bool: ...

    def deletion_runtime_snapshot(
        self, project_id: UUID
    ) -> tuple[Sequence[Deployment], ProjectRuntime | None]: ...

    def deletion_mutation_allowed(
        self, claim: ProjectDeletionJobClaim, *, require_route_removed: bool
    ) -> bool: ...

    def finalize_deletion(self, claim: ProjectDeletionJobClaim) -> None: ...


class DeletionRouteRepository(Protocol):
    def disable_for_deletion(self, project_id: UUID) -> Any: ...

    def deletion_is_applied(self, project_id: UUID) -> bool: ...


class RuntimeTeardown(Protocol):
    def teardown(
        self,
        project_id: UUID,
        deployments: Sequence[Deployment],
        runtime: ProjectRuntime | None,
        *,
        mutation_guard: Callable[[], bool],
        heartbeat: Callable[[], bool | None],
    ) -> None: ...

    def verify_absent(
        self,
        project_id: UUID,
        deployments: Sequence[Deployment],
        runtime: ProjectRuntime | None,
        *,
        mutation_guard: Callable[[], bool],
        heartbeat: Callable[[], bool | None],
    ) -> None: ...


class DeletionSecretStore(Protocol):
    def project_operation_lock(
        self, project_id: UUID, *, blocking: bool = True
    ) -> AbstractContextManager[None]: ...

    def delete_project_subtree(self, project_id: UUID) -> None: ...

    def project_subtree_absent(self, project_id: UUID) -> bool: ...


class DatabaseDeletionGateway(Protocol):
    def get_resource(self, project_id: UUID) -> Any | None: ...

    def try_operation_lock(self, resource_id: UUID) -> AbstractContextManager[bool]: ...

    def quiesce(self, resource: Any) -> None: ...

    def drop_database(self, resource: Any) -> None: ...

    def drop_role(self, resource: Any) -> None: ...

    def verify_absent(self, resource: Any) -> None: ...


class ProjectDatabaseDeletionAdapter:
    def __init__(self, repository: Any, provisioner: Any | None) -> None:
        self._repository = repository
        self._provisioner = provisioner

    def get_resource(self, project_id: UUID) -> Any | None:
        return self._repository.get_for_project(project_id)

    @contextmanager
    def try_operation_lock(self, resource_id: UUID):
        if self._provisioner is None:
            raise RuntimeFailure(
                "DELETION",
                "PROJECT_DATABASE_DELETION_UNAVAILABLE",
                cleanup_candidate=False,
            )
        try:
            with self._provisioner.operation_lock(resource_id, blocking=False):
                yield True
        except ProjectDatabaseProvisioningError as error:
            if error.code == "OPERATION_LOCK_BUSY":
                yield False
                return
            raise _database_failure(error) from error

    def quiesce(self, resource: Any) -> None:
        provisioner = self._require_provisioner()
        self._call(
            provisioner.quiesce,
            resource.id,
            resource.database_name,
            resource.role_name,
        )

    def drop_database(self, resource: Any) -> None:
        provisioner = self._require_provisioner()
        self._call(
            provisioner.drop_database,
            resource.id,
            resource.database_name,
        )

    def drop_role(self, resource: Any) -> None:
        provisioner = self._require_provisioner()
        self._call(provisioner.drop_role, resource.id, resource.role_name)

    def verify_absent(self, resource: Any) -> None:
        provisioner = self._require_provisioner()
        self._call(
            provisioner.verify_absent,
            resource.id,
            resource.database_name,
            resource.role_name,
        )

    def _require_provisioner(self) -> Any:
        if self._provisioner is None:
            raise RuntimeFailure(
                "DELETION",
                "PROJECT_DATABASE_DELETION_UNAVAILABLE",
                cleanup_candidate=False,
            )
        return self._provisioner

    def _call(self, operation: Callable[..., None], *arguments: Any) -> None:
        try:
            operation(*arguments)
        except ProjectDatabaseProvisioningError as error:
            raise _database_failure(error) from error


def _database_failure(error: ProjectDatabaseProvisioningError) -> RuntimeFailure:
    codes = {
        "OWNERSHIP_CONFLICT": ("PROJECT_DATABASE_OWNERSHIP_CONFLICT", False),
        "RESOURCES_REAPPEARED": ("PROJECT_DATABASE_RESOURCES_REAPPEARED", False),
        "SESSIONS_ACTIVE": ("PROJECT_DATABASE_SESSIONS_ACTIVE", True),
    }
    code, retryable = codes.get(
        error.code,
        (f"PROJECT_DATABASE_{error.code}", True),
    )
    return RuntimeFailure(
        "DELETION",
        code,
        retryable=retryable,
        cleanup_candidate=False,
    )


class ProjectDeletionWorker:
    def __init__(
        self,
        projects: DeletionProjectRepository,
        routes: DeletionRouteRepository,
        runtime: RuntimeTeardown,
        secrets: DeletionSecretStore,
        databases: DatabaseDeletionGateway,
        *,
        worker_id: str,
        lease_duration: timedelta,
        max_attempts: int,
        retry_base_delay: timedelta,
    ) -> None:
        self._projects = projects
        self._routes = routes
        self._runtime = runtime
        self._secrets = secrets
        self._databases = databases
        self._worker_id = worker_id
        self._lease_duration = lease_duration
        self._max_attempts = max_attempts
        self._retry_base_delay = retry_base_delay

    def run_once(self) -> bool:
        claim = self._projects.claim_next_deletion(self._worker_id, self._lease_duration)
        if claim is None:
            return False
        if claim.job.attempts > self._max_attempts:
            self._persist_failure(claim, "PROJECT_DELETION_ATTEMPTS_EXHAUSTED", retryable=True)
            return True
        try:
            self._run_claim(claim)
        except ProjectDeletionClaimLostError:
            return True
        except RuntimeFailure as error:
            self._persist_failure(claim, error.code, retryable=error.retryable)
        except SecretStoreError:
            self._persist_failure(claim, "PROJECT_SECRET_CLEANUP_FAILED", retryable=False)
        return True

    def _run_claim(self, claim: ProjectDeletionJobClaim) -> None:
        while True:
            phase = claim.job.phase
            if phase is ProjectDeletionPhase.REQUESTED:
                claim = self._advance(
                    claim,
                    ProjectDeletionPhase.REQUESTED,
                    ProjectDeletionPhase.WAITING_FOR_OPERATIONS,
                )
                continue
            if phase is ProjectDeletionPhase.WAITING_FOR_OPERATIONS:
                if not self._operations_drained(claim):
                    self._defer(claim)
                    return
                claim = self._advance(
                    claim,
                    phase,
                    ProjectDeletionPhase.ROUTE_DISABLING,
                )
                continue
            if phase is ProjectDeletionPhase.ROUTE_DISABLING:
                self._authorize(claim, require_route_removed=False)
                self._routes.disable_for_deletion(claim.job.project_id)
                if not self._routes.deletion_is_applied(claim.job.project_id):
                    self._defer(claim)
                    return
                claim = self._advance(claim, phase, ProjectDeletionPhase.ROUTE_REMOVED)
                continue
            if phase is ProjectDeletionPhase.ROUTE_REMOVED:
                if not self._routes.deletion_is_applied(claim.job.project_id):
                    self._defer(claim)
                    return
                claim = self._advance(claim, phase, ProjectDeletionPhase.RUNTIME_CLEANUP)
                continue
            if phase is ProjectDeletionPhase.RUNTIME_CLEANUP:
                self._authorize(claim, require_route_removed=True)
                deployments, runtime = self._projects.deletion_runtime_snapshot(
                    claim.job.project_id
                )
                self._runtime.teardown(
                    claim.job.project_id,
                    deployments,
                    runtime,
                    mutation_guard=lambda claim=claim: self._guard(
                        claim, require_route_removed=True
                    ),
                    heartbeat=lambda claim=claim: self._heartbeat(claim),
                )
                claim = self._advance(claim, phase, ProjectDeletionPhase.DATABASE_QUIESCING)
                continue
            if phase is ProjectDeletionPhase.DATABASE_QUIESCING:
                self._run_database_phase(claim, self._databases.quiesce)
                claim = self._advance(claim, phase, ProjectDeletionPhase.DATABASE_DROP_DATABASE)
                continue
            if phase is ProjectDeletionPhase.DATABASE_DROP_DATABASE:
                self._run_database_phase(claim, self._databases.drop_database)
                claim = self._advance(claim, phase, ProjectDeletionPhase.DATABASE_DROP_ROLE)
                continue
            if phase is ProjectDeletionPhase.DATABASE_DROP_ROLE:
                self._run_database_phase(claim, self._databases.drop_role)
                claim = self._advance(claim, phase, ProjectDeletionPhase.SECRET_CLEANUP)
                continue
            if phase is ProjectDeletionPhase.SECRET_CLEANUP:
                try:
                    with self._secrets.project_operation_lock(claim.job.project_id, blocking=False):
                        self._authorize(claim, require_route_removed=True)
                        self._secrets.delete_project_subtree(claim.job.project_id)
                except SecretStoreBusyError as error:
                    raise RuntimeFailure(
                        "DELETION",
                        "PROJECT_SECRET_OPERATION_ACTIVE",
                        retryable=True,
                        cleanup_candidate=False,
                    ) from error
                claim = self._advance(claim, phase, ProjectDeletionPhase.METADATA_DELETE)
                continue
            if phase is ProjectDeletionPhase.METADATA_DELETE:
                self._verify_final_absence(claim)
                self._projects.finalize_deletion(claim)
                return
            raise RuntimeError(f"unsupported deletion phase: {phase}")

    def _operations_drained(self, claim: ProjectDeletionJobClaim) -> bool:
        if not self._projects.deletion_operations_drained(claim.job.project_id):
            return False
        try:
            with self._secrets.project_operation_lock(claim.job.project_id, blocking=False):
                pass
        except SecretStoreError:
            return False
        resource = self._databases.get_resource(claim.job.project_id)
        if resource is None:
            return True
        with self._databases.try_operation_lock(resource.id) as acquired:
            return acquired

    def _run_database_phase(
        self, claim: ProjectDeletionJobClaim, operation: Callable[[Any], None]
    ) -> None:
        resource = self._validated_database_resource(claim)
        if resource is None:
            return
        failure: RuntimeFailure | None = None
        acquired_lock = False
        with self._databases.try_operation_lock(resource.id) as acquired:
            acquired_lock = acquired
            if acquired:
                try:
                    self._authorize(claim, require_route_removed=True)
                    operation(resource)
                except RuntimeFailure as error:
                    failure = error
        if not acquired_lock:
            raise RuntimeFailure(
                "DELETION",
                "PROJECT_DATABASE_OPERATION_ACTIVE",
                retryable=True,
                cleanup_candidate=False,
            )
        if failure is not None:
            raise failure

    def _validated_database_resource(self, claim: ProjectDeletionJobClaim) -> Any | None:
        resource = self._databases.get_resource(claim.job.project_id)
        if (resource is not None) != claim.job.delete_managed_database:
            raise RuntimeFailure(
                "DELETION",
                "PROJECT_DATABASE_AUTHORIZATION_MISMATCH",
                retryable=False,
                cleanup_candidate=False,
            )
        return resource

    def _verify_final_absence(self, claim: ProjectDeletionJobClaim) -> None:
        self._authorize(claim, require_route_removed=True)
        deployments, runtime = self._projects.deletion_runtime_snapshot(claim.job.project_id)
        self._runtime.verify_absent(
            claim.job.project_id,
            deployments,
            runtime,
            mutation_guard=lambda claim=claim: self._guard(claim, require_route_removed=True),
            heartbeat=lambda claim=claim: self._heartbeat(claim),
        )
        resource = self._validated_database_resource(claim)
        if resource is not None:
            failure: RuntimeFailure | None = None
            acquired_lock = False
            with self._databases.try_operation_lock(resource.id) as acquired:
                acquired_lock = acquired
                if acquired:
                    try:
                        self._authorize(claim, require_route_removed=True)
                        self._databases.verify_absent(resource)
                    except RuntimeFailure as error:
                        failure = error
            if not acquired_lock:
                raise RuntimeFailure(
                    "DELETION",
                    "PROJECT_DATABASE_OPERATION_ACTIVE",
                    retryable=True,
                    cleanup_candidate=False,
                )
            if failure is not None:
                raise failure
        secret_absent = True
        try:
            with self._secrets.project_operation_lock(claim.job.project_id, blocking=False):
                self._authorize(claim, require_route_removed=True)
                secret_absent = self._secrets.project_subtree_absent(claim.job.project_id)
        except SecretStoreBusyError as error:
            raise RuntimeFailure(
                "DELETION",
                "PROJECT_SECRET_OPERATION_ACTIVE",
                retryable=True,
                cleanup_candidate=False,
            ) from error
        if not secret_absent:
            raise RuntimeFailure(
                "DELETION",
                "PROJECT_SECRET_RESOURCES_REAPPEARED",
                retryable=False,
                cleanup_candidate=False,
            )

    def _advance(
        self,
        claim: ProjectDeletionJobClaim,
        expected: ProjectDeletionPhase,
        next_phase: ProjectDeletionPhase,
    ) -> ProjectDeletionJobClaim:
        job = self._projects.advance_deletion(claim, expected, next_phase)
        return replace(claim, job=job)

    def _authorize(self, claim: ProjectDeletionJobClaim, *, require_route_removed: bool) -> None:
        if not self._heartbeat(claim) or not self._guard(
            claim, require_route_removed=require_route_removed
        ):
            raise ProjectDeletionClaimLostError

    def _guard(self, claim: ProjectDeletionJobClaim, *, require_route_removed: bool) -> bool:
        return self._projects.deletion_mutation_allowed(
            claim, require_route_removed=require_route_removed
        )

    def _heartbeat(self, claim: ProjectDeletionJobClaim) -> bool:
        try:
            self._projects.renew_deletion(claim, self._lease_duration)
        except ProjectDeletionClaimLostError:
            return False
        return True

    def _reschedule(self, claim: ProjectDeletionJobClaim, code: str) -> None:
        delay = self._retry_base_delay * min(2 ** max(claim.job.attempts - 1, 0), 64)
        self._projects.reschedule_deletion(claim, datetime.now(UTC) + delay, code)

    def _defer(self, claim: ProjectDeletionJobClaim) -> None:
        delay = self._retry_base_delay * min(2 ** max(claim.job.attempts - 1, 0), 64)
        self._projects.defer_deletion(claim, datetime.now(UTC) + delay)

    def _handle_failure(
        self, claim: ProjectDeletionJobClaim, code: str, *, retryable: bool
    ) -> None:
        if retryable and claim.job.attempts < self._max_attempts:
            self._reschedule(claim, code)
            return
        self._projects.fail_deletion(claim, code, retryable=retryable)

    def _persist_failure(
        self, claim: ProjectDeletionJobClaim, code: str, *, retryable: bool
    ) -> None:
        with suppress(ProjectDeletionClaimLostError):
            self._handle_failure(claim, code, retryable=retryable)
