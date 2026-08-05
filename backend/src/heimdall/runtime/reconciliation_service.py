from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from heimdall.common.errors import AppError
from heimdall.deployments.models import Deployment, DeploymentStatus
from heimdall.deployments.service import DeploymentService
from heimdall.runtime.reconciliation import (
    ReconciliationAction,
    ReconciliationInProgressError,
    ReconciliationRequester,
    ReconciliationResult,
    RuntimeReconciliation,
    RuntimeReconciliationRepository,
)


@dataclass(frozen=True, slots=True)
class RuntimeReconciliationView:
    deployment_id: UUID
    state: str
    action: ReconciliationAction
    requested_by: ReconciliationRequester
    result: ReconciliationResult | None
    result_code: str | None
    attempts: int
    available_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class RuntimeReconciliationService:
    def __init__(
        self,
        repository: RuntimeReconciliationRepository,
        deployments: DeploymentService,
        retention_duration: timedelta,
    ) -> None:
        if retention_duration <= timedelta(0):
            raise ValueError("retention duration must be positive")
        self._repository = repository
        self._deployments = deployments
        self._retention_duration = retention_duration

    def get(self, deployment_id: UUID) -> RuntimeReconciliationView:
        deployment = self._deployments.get(deployment_id)
        item = self._repository.get(deployment_id)
        if item is not None:
            return _view(item)
        self._require_uncertain(deployment)
        assert deployment.terminal_at is not None
        return RuntimeReconciliationView(
            deployment_id=deployment.id,
            state="RETAINED",
            action=ReconciliationAction.RECONCILE,
            requested_by=ReconciliationRequester.SYSTEM,
            result=None,
            result_code=None,
            attempts=0,
            available_at=deployment.terminal_at + self._retention_duration,
            updated_at=deployment.updated_at,
            completed_at=None,
        )

    def request(
        self,
        deployment_id: UUID,
        action: ReconciliationAction,
        confirmation: str | None,
    ) -> RuntimeReconciliationView:
        deployment = self._deployments.get(deployment_id)
        self._require_uncertain(deployment)
        if action is ReconciliationAction.FORCE_CLEANUP and confirmation != str(deployment.id):
            raise AppError(
                422,
                "DEPLOYMENT_CONFIRMATION_MISMATCH",
                "Enter the full deployment ID to confirm forced cleanup",
            )
        if action is ReconciliationAction.RECONCILE and confirmation is not None:
            raise AppError(
                422,
                "UNEXPECTED_DEPLOYMENT_CONFIRMATION",
                "Safe reconciliation does not accept a cleanup confirmation",
            )
        try:
            item = self._repository.request(
                deployment.id,
                action,
                ReconciliationRequester.ADMIN,
            )
        except ReconciliationInProgressError as error:
            raise AppError(
                409,
                "RUNTIME_RECONCILIATION_IN_PROGRESS",
                "Wait for the current runtime reconciliation to finish",
            ) from error
        return _view(item)

    @staticmethod
    def _require_uncertain(deployment: Deployment) -> None:
        if not (
            deployment.status is DeploymentStatus.FAILED
            and deployment.failure_stage == "RECOVERY"
            and deployment.failure_code == "RECOVERY_STATE_UNCERTAIN"
            and deployment.terminal_at is not None
        ):
            raise AppError(
                409,
                "RUNTIME_RECONCILIATION_NOT_REQUIRED",
                "Only an uncertain failed deployment can be reconciled",
            )


def _view(item: RuntimeReconciliation) -> RuntimeReconciliationView:
    return RuntimeReconciliationView(
        deployment_id=item.deployment_id,
        state=item.state.value,
        action=item.action,
        requested_by=item.requested_by,
        result=item.result,
        result_code=item.result_code,
        attempts=item.attempts,
        available_at=item.available_at,
        updated_at=item.updated_at,
        completed_at=item.completed_at,
    )
