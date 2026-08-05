from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from test_runtime_models import runtime_deployment

from heimdall.common.errors import AppError
from heimdall.deployments.models import Deployment, DeploymentStatus
from heimdall.deployments.worker import RecoveryDisposition, RuntimeFailure
from heimdall.runtime.reconciliation import (
    ReconciliationAction,
    ReconciliationRequester,
    ReconciliationResult,
    ReconciliationState,
    RuntimeReconciliation,
    RuntimeReconciliationClaim,
)
from heimdall.runtime.reconciliation_service import RuntimeReconciliationService
from heimdall.runtime.reconciliation_worker import RuntimeReconciliationWorker
from heimdall.runtime.service import DockerDeploymentProcessor


def uncertain_deployment() -> Deployment:
    now = datetime.now(UTC)
    return replace(
        runtime_deployment(),
        status=DeploymentStatus.FAILED,
        failure_stage="RECOVERY",
        failure_code="RECOVERY_STATE_UNCERTAIN",
        updated_at=now,
        terminal_at=now,
    )


def reconciliation(
    deployment_id: UUID,
    *,
    action: ReconciliationAction = ReconciliationAction.RECONCILE,
    attempts: int = 1,
) -> RuntimeReconciliation:
    now = datetime.now(UTC)
    return RuntimeReconciliation(
        deployment_id=deployment_id,
        action=action,
        requested_by=ReconciliationRequester.SYSTEM,
        state=ReconciliationState.CLAIMED,
        result=None,
        result_code=None,
        attempts=attempts,
        available_at=now,
        created_at=now,
        updated_at=now,
        completed_at=None,
    )


class MemoryReconciliations:
    def __init__(self, item: RuntimeReconciliation | None = None) -> None:
        self.item = item
        self.available = item is not None
        self.resolved: tuple[ReconciliationResult, str] | None = None
        self.blocked: str | None = None
        self.retried: str | None = None
        self.heartbeats = 0

    def get(self, deployment_id: UUID) -> RuntimeReconciliation | None:
        return (
            self.item
            if self.item is not None and self.item.deployment_id == deployment_id
            else None
        )

    def request(self, deployment_id, action, requested_by) -> RuntimeReconciliation:
        now = datetime.now(UTC)
        self.item = RuntimeReconciliation(
            deployment_id=deployment_id,
            action=action,
            requested_by=requested_by,
            state=ReconciliationState.PENDING,
            result=None,
            result_code=None,
            attempts=0,
            available_at=now,
            created_at=now,
            updated_at=now,
            completed_at=None,
        )
        return self.item

    def schedule_automatic(self, candidates) -> None:
        return

    def claim_next(self, worker_id, lease_duration):
        if not self.available or self.item is None:
            return None
        self.available = False
        return RuntimeReconciliationClaim(
            reconciliation=self.item,
            token=uuid4(),
            worker_id=worker_id,
            lease_expires_at=datetime.now(UTC) + lease_duration,
        )

    def renew(self, claim, lease_duration):
        self.heartbeats += 1
        return datetime.now(UTC) + lease_duration

    def resolve(self, claim, result, result_code):
        self.resolved = (result, result_code)
        return self.item

    def block(self, claim, result_code):
        self.blocked = result_code
        return self.item

    def retry(self, claim, available_at, result_code):
        self.retried = result_code
        return self.item


class MemoryDeployments:
    def __init__(self, item: Deployment) -> None:
        self.item = item
        self.reconciled = False

    def get(self, deployment_id: UUID) -> Deployment:
        assert deployment_id == self.item.id
        return self.item

    def list_uncertain_before(self, cutoff):
        return [self.item] if self.item.terminal_at <= cutoff else []

    def reconcile_succeeded(self, deployment_id: UUID) -> Deployment:
        assert deployment_id == self.item.id
        self.reconciled = True
        self.item = replace(
            self.item,
            status=DeploymentStatus.SUCCEEDED,
            failure_stage=None,
            failure_code=None,
        )
        return self.item


class Processor:
    def __init__(
        self,
        disposition: RecoveryDisposition,
        failure: RuntimeFailure | None = None,
    ) -> None:
        self.disposition = disposition
        self.failure = failure
        self.cleaned = False

    def recover(self, deployment, progress):
        progress.heartbeat()
        return self.disposition

    def cleanup_candidate_verified(self, deployment, progress):
        progress.heartbeat()
        if self.failure is not None:
            raise self.failure
        self.cleaned = True


def worker(
    repository: MemoryReconciliations,
    deployments: MemoryDeployments,
    processor: Processor,
) -> RuntimeReconciliationWorker:
    return RuntimeReconciliationWorker(
        repository,
        deployments,
        processor,
        worker_id="reconciliation-worker",
        lease_duration=timedelta(minutes=1),
        retention_duration=timedelta(hours=72),
    )


def test_active_preserved_generation_is_adopted_without_cleanup() -> None:
    deployment = uncertain_deployment()
    repository = MemoryReconciliations(reconciliation(deployment.id))
    deployments = MemoryDeployments(deployment)
    processor = Processor(RecoveryDisposition.ACTIVE)

    assert worker(repository, deployments, processor).run_once() is True

    assert deployments.reconciled is True
    assert processor.cleaned is False
    assert repository.resolved == (
        ReconciliationResult.ACTIVE,
        "ACTIVE_GENERATION_RECONCILED",
    )


def test_safe_inactive_candidate_is_cleaned() -> None:
    deployment = uncertain_deployment()
    repository = MemoryReconciliations(reconciliation(deployment.id))
    processor = Processor(RecoveryDisposition.SAFE_TO_RETRY)

    worker(repository, MemoryDeployments(deployment), processor).run_once()

    assert processor.cleaned is True
    assert repository.resolved == (
        ReconciliationResult.CLEANED,
        "INACTIVE_CANDIDATE_CLEANED",
    )


def test_automatic_uncertain_reconciliation_preserves_candidate() -> None:
    deployment = uncertain_deployment()
    repository = MemoryReconciliations(reconciliation(deployment.id))
    processor = Processor(RecoveryDisposition.UNCERTAIN)

    worker(repository, MemoryDeployments(deployment), processor).run_once()

    assert processor.cleaned is False
    assert repository.blocked == "RECOVERY_STATE_UNCERTAIN"


def test_confirmed_force_cleanup_can_remove_an_uncertain_candidate() -> None:
    deployment = uncertain_deployment()
    repository = MemoryReconciliations(
        reconciliation(deployment.id, action=ReconciliationAction.FORCE_CLEANUP)
    )
    processor = Processor(RecoveryDisposition.UNCERTAIN)

    worker(repository, MemoryDeployments(deployment), processor).run_once()

    assert processor.cleaned is True
    assert repository.resolved == (
        ReconciliationResult.CLEANED,
        "FORCED_CANDIDATE_CLEANUP",
    )


def test_retryable_cleanup_failure_is_bounded() -> None:
    deployment = uncertain_deployment()
    repository = MemoryReconciliations(reconciliation(deployment.id, attempts=1))
    processor = Processor(
        RecoveryDisposition.SAFE_TO_RETRY,
        RuntimeFailure(
            "RECONCILIATION",
            "CANDIDATE_RESOURCE_OBSERVATION_FAILED",
            retryable=True,
            cleanup_candidate=False,
        ),
    )

    worker(repository, MemoryDeployments(deployment), processor).run_once()

    assert repository.retried == "CANDIDATE_RESOURCE_OBSERVATION_FAILED"
    assert repository.resolved is None


class DeploymentLookup:
    def __init__(self, item: Deployment) -> None:
        self.item = item

    def get(self, deployment_id: UUID) -> Deployment:
        assert deployment_id == self.item.id
        return self.item


def test_service_exposes_retention_deadline_before_a_job_exists() -> None:
    deployment = uncertain_deployment()
    service = RuntimeReconciliationService(
        MemoryReconciliations(),
        DeploymentLookup(deployment),
        timedelta(hours=72),
    )

    view = service.get(deployment.id)

    assert view.state == "RETAINED"
    assert view.available_at == deployment.terminal_at + timedelta(hours=72)


def test_force_cleanup_requires_the_full_deployment_id() -> None:
    deployment = uncertain_deployment()
    service = RuntimeReconciliationService(
        MemoryReconciliations(),
        DeploymentLookup(deployment),
        timedelta(hours=72),
    )

    with pytest.raises(AppError) as raised:
        service.request(deployment.id, ReconciliationAction.FORCE_CLEANUP, "wrong")

    assert raised.value.code == "DEPLOYMENT_CONFIRMATION_MISMATCH"


def test_force_cleanup_request_is_queued_after_exact_confirmation() -> None:
    deployment = uncertain_deployment()
    service = RuntimeReconciliationService(
        MemoryReconciliations(),
        DeploymentLookup(deployment),
        timedelta(hours=72),
    )

    view = service.request(
        deployment.id,
        ReconciliationAction.FORCE_CLEANUP,
        str(deployment.id),
    )

    assert view.state == "PENDING"
    assert view.action is ReconciliationAction.FORCE_CLEANUP
    assert view.requested_by is ReconciliationRequester.ADMIN


def test_verified_cleanup_refuses_a_database_active_generation(tmp_path) -> None:
    deployment = uncertain_deployment()

    class ActiveGateway:
        def is_active(self, item) -> bool:
            return item.id == deployment.id

        def rollback_candidate(self, item) -> None:
            raise AssertionError("active gateway must not be rolled back")

    class Docker:
        def cleanup_candidate_verified(self, item, runtime, progress) -> None:
            raise AssertionError("active candidate must not be deleted")

    processor = DockerDeploymentProcessor(
        projects=None,
        git=None,
        docker=Docker(),
        activator=ActiveGateway(),
        secret_store=None,
        workspace_root=tmp_path / "workspaces",
    )

    with pytest.raises(RuntimeFailure) as raised:
        processor.cleanup_candidate_verified(deployment, progress=None)

    assert raised.value.code == "ACTIVE_GENERATION_CANNOT_BE_CLEANED"
