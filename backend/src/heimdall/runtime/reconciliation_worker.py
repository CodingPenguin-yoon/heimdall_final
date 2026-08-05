from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from heimdall.common.errors import AppError
from heimdall.deployments.models import Deployment
from heimdall.deployments.worker import RecoveryDisposition, RuntimeFailure
from heimdall.runtime.reconciliation import (
    ReconciliationAction,
    ReconciliationClaimLostError,
    ReconciliationResult,
    RuntimeReconciliationClaim,
    RuntimeReconciliationRepository,
)


class ReconciliationProcessor(Protocol):
    def recover(self, deployment: Deployment, progress) -> RecoveryDisposition: ...

    def cleanup_candidate_verified(self, deployment: Deployment, progress) -> None: ...


class ReconciliationDeployments(Protocol):
    def list_uncertain_before(self, cutoff: datetime) -> Sequence[Deployment]: ...

    def get(self, deployment_id: UUID) -> Deployment: ...

    def reconcile_succeeded(self, deployment_id: UUID) -> Deployment: ...


class ReconciliationProgress:
    def __init__(
        self,
        repository: RuntimeReconciliationRepository,
        claim: RuntimeReconciliationClaim,
        lease_duration: timedelta,
    ) -> None:
        self._repository = repository
        self._claim = claim
        self._lease_duration = lease_duration

    def heartbeat(self) -> None:
        self._repository.renew(self._claim, self._lease_duration)


class RuntimeReconciliationWorker:
    def __init__(
        self,
        repository: RuntimeReconciliationRepository,
        deployments: ReconciliationDeployments,
        processor: ReconciliationProcessor,
        *,
        worker_id: str,
        lease_duration: timedelta,
        retention_duration: timedelta,
        max_attempts: int = 3,
        retry_base_delay: timedelta = timedelta(seconds=5),
    ) -> None:
        if not worker_id or len(worker_id) > 128:
            raise ValueError("worker_id must be between 1 and 128 characters")
        if lease_duration <= timedelta(0):
            raise ValueError("lease duration must be positive")
        if retention_duration <= timedelta(0):
            raise ValueError("retention duration must be positive")
        if max_attempts < 1:
            raise ValueError("max attempts must be positive")
        self._repository = repository
        self._deployments = deployments
        self._processor = processor
        self._worker_id = worker_id
        self._lease_duration = lease_duration
        self._retention_duration = retention_duration
        self._max_attempts = max_attempts
        self._retry_base_delay = retry_base_delay

    def run_once(self) -> bool:
        now = datetime.now(UTC)
        eligible = self._deployments.list_uncertain_before(now - self._retention_duration)
        self._repository.schedule_automatic(
            tuple(
                (item.id, item.terminal_at + self._retention_duration)
                for item in eligible
                if item.terminal_at is not None
            )
        )
        claim = self._repository.claim_next(self._worker_id, self._lease_duration)
        if claim is None:
            return False
        progress = ReconciliationProgress(self._repository, claim, self._lease_duration)
        try:
            if claim.reconciliation.attempts > self._max_attempts:
                self._repository.block(claim, "RECONCILIATION_ATTEMPTS_EXHAUSTED")
                return True
            deployment = self._deployments.get(claim.reconciliation.deployment_id)
            disposition = self._processor.recover(deployment, progress)
            if disposition is RecoveryDisposition.ACTIVE:
                progress.heartbeat()
                self._deployments.reconcile_succeeded(deployment.id)
                self._repository.resolve(
                    claim,
                    ReconciliationResult.ACTIVE,
                    "ACTIVE_GENERATION_RECONCILED",
                )
                return True
            if disposition is RecoveryDisposition.SAFE_TO_RETRY:
                self._processor.cleanup_candidate_verified(deployment, progress)
                progress.heartbeat()
                self._repository.resolve(
                    claim,
                    ReconciliationResult.CLEANED,
                    "INACTIVE_CANDIDATE_CLEANED",
                )
                return True
            if claim.reconciliation.action is ReconciliationAction.FORCE_CLEANUP:
                self._processor.cleanup_candidate_verified(deployment, progress)
                progress.heartbeat()
                self._repository.resolve(
                    claim,
                    ReconciliationResult.CLEANED,
                    "FORCED_CANDIDATE_CLEANUP",
                )
                return True
            self._repository.block(claim, "RECOVERY_STATE_UNCERTAIN")
        except ReconciliationClaimLostError:
            return True
        except AppError:
            self._block(claim, "RECONCILIATION_DEPLOYMENT_CONFLICT")
        except RuntimeFailure as failure:
            self._handle_failure(claim, failure)
        except Exception:
            self._handle_failure(
                claim,
                RuntimeFailure(
                    "RECONCILIATION",
                    "UNEXPECTED_RECONCILIATION_FAILURE",
                    retryable=True,
                    cleanup_candidate=False,
                ),
            )
        return True

    def _handle_failure(self, claim: RuntimeReconciliationClaim, failure: RuntimeFailure) -> None:
        try:
            if failure.retryable and claim.reconciliation.attempts < self._max_attempts:
                delay = self._retry_base_delay * (2 ** (claim.reconciliation.attempts - 1))
                self._repository.retry(
                    claim,
                    datetime.now(UTC) + delay,
                    failure.code,
                )
            else:
                self._repository.block(claim, failure.code)
        except ReconciliationClaimLostError:
            return

    def _block(self, claim: RuntimeReconciliationClaim, result_code: str) -> None:
        try:
            self._repository.block(claim, result_code)
        except ReconciliationClaimLostError:
            return
