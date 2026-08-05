from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class ReconciliationAction(StrEnum):
    RECONCILE = "RECONCILE"
    FORCE_CLEANUP = "FORCE_CLEANUP"


class ReconciliationRequester(StrEnum):
    SYSTEM = "SYSTEM"
    ADMIN = "ADMIN"


class ReconciliationState(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    RESOLVED = "RESOLVED"
    BLOCKED = "BLOCKED"


class ReconciliationResult(StrEnum):
    ACTIVE = "ACTIVE"
    CLEANED = "CLEANED"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True, slots=True)
class RuntimeReconciliation:
    deployment_id: UUID
    action: ReconciliationAction
    requested_by: ReconciliationRequester
    state: ReconciliationState
    result: ReconciliationResult | None
    result_code: str | None
    attempts: int
    available_at: datetime
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class RuntimeReconciliationClaim:
    reconciliation: RuntimeReconciliation
    token: UUID
    worker_id: str
    lease_expires_at: datetime


class ReconciliationInProgressError(RuntimeError):
    pass


class ReconciliationClaimLostError(RuntimeError):
    pass


class RuntimeReconciliationRepository(Protocol):
    def get(self, deployment_id: UUID) -> RuntimeReconciliation | None: ...

    def request(
        self,
        deployment_id: UUID,
        action: ReconciliationAction,
        requested_by: ReconciliationRequester,
    ) -> RuntimeReconciliation: ...

    def schedule_automatic(self, candidates: Sequence[tuple[UUID, datetime]]) -> None: ...

    def claim_next(
        self,
        worker_id: str,
        lease_duration: timedelta,
    ) -> RuntimeReconciliationClaim | None: ...

    def renew(self, claim: RuntimeReconciliationClaim, lease_duration: timedelta) -> datetime: ...

    def resolve(
        self,
        claim: RuntimeReconciliationClaim,
        result: ReconciliationResult,
        result_code: str,
    ) -> RuntimeReconciliation: ...

    def block(
        self, claim: RuntimeReconciliationClaim, result_code: str
    ) -> RuntimeReconciliation: ...

    def retry(
        self,
        claim: RuntimeReconciliationClaim,
        available_at: datetime,
        result_code: str,
    ) -> RuntimeReconciliation: ...
