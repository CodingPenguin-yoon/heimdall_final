from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from heimdall.deployments.models import (
    Deployment,
    DeploymentClaimLostError,
    DeploymentJobClaim,
    DeploymentStatus,
)
from heimdall.deployments.repository import DeploymentRepository


@dataclass(frozen=True, slots=True)
class RuntimeFailure(RuntimeError):
    stage: str
    code: str
    retryable: bool = False
    cleanup_candidate: bool = True


class RecoveryDisposition(StrEnum):
    ACTIVE = "ACTIVE"
    SAFE_TO_RETRY = "SAFE_TO_RETRY"
    UNCERTAIN = "UNCERTAIN"


class RuntimeProcessor(Protocol):
    def recover(self, deployment: Deployment, progress: RuntimeProgress) -> RecoveryDisposition: ...

    def process(self, deployment: Deployment, progress: RuntimeProgress) -> None: ...

    def cleanup_candidate(self, deployment: Deployment) -> None: ...


class RuntimeProgress:
    def __init__(
        self,
        repository: DeploymentRepository,
        claim: DeploymentJobClaim,
        lease_duration: timedelta,
    ) -> None:
        self._repository = repository
        self._claim = claim
        self._lease_duration = lease_duration

    def heartbeat(self) -> None:
        self._repository.renew(self._claim, self._lease_duration)

    def stage(self, status: DeploymentStatus, code: str, message: str) -> None:
        self._repository.renew(self._claim, self._lease_duration)
        self._repository.advance(
            self._claim,
            status,
            event_code=code,
            event_message=message,
        )


class DeploymentWorker:
    def __init__(
        self,
        repository: DeploymentRepository,
        processor: RuntimeProcessor,
        *,
        worker_id: str,
        lease_duration: timedelta,
        max_attempts: int = 3,
        retry_base_delay: timedelta = timedelta(seconds=5),
    ) -> None:
        if not worker_id or len(worker_id) > 128:
            raise ValueError("worker_id must be between 1 and 128 characters")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._repository = repository
        self._processor = processor
        self._worker_id = worker_id
        self._lease_duration = lease_duration
        self._max_attempts = max_attempts
        self._retry_base_delay = retry_base_delay

    def run_once(self) -> bool:
        claim = self._repository.claim_next(self._worker_id, self._lease_duration)
        if claim is None:
            return False
        progress = RuntimeProgress(self._repository, claim, self._lease_duration)
        try:
            if claim.attempts > 1:
                recovery = self._processor.recover(claim.deployment, progress)
                if recovery is RecoveryDisposition.ACTIVE:
                    progress.heartbeat()
                    self._repository.succeed(claim)
                    return True
                if recovery is RecoveryDisposition.UNCERTAIN:
                    self._handle_failure(
                        claim,
                        RuntimeFailure(
                            "RECOVERY",
                            "RECOVERY_STATE_UNCERTAIN",
                            retryable=True,
                            cleanup_candidate=False,
                        ),
                    )
                    return True
                if claim.attempts > self._max_attempts:
                    self._handle_failure(
                        claim,
                        RuntimeFailure("RECOVERY", "WORKER_RECOVERY_EXHAUSTED"),
                    )
                    return True
            self._processor.process(claim.deployment, progress)
            progress.heartbeat()
            self._repository.succeed(claim)
        except DeploymentClaimLostError:
            return True
        except RuntimeFailure as failure:
            self._handle_failure(claim, failure)
        except Exception:
            self._handle_failure(
                claim,
                RuntimeFailure("RUNTIME", "UNEXPECTED_RUNTIME_FAILURE", retryable=False),
            )
        return True

    def _handle_failure(self, claim: DeploymentJobClaim, failure: RuntimeFailure) -> None:
        if failure.cleanup_candidate:
            try:
                self._processor.cleanup_candidate(claim.deployment)
            except DeploymentClaimLostError:
                return
            except Exception:
                failure = RuntimeFailure("CLEANUP", "CANDIDATE_CLEANUP_FAILED", retryable=True)
        try:
            if failure.retryable and claim.attempts < self._max_attempts:
                delay = self._retry_base_delay * (2 ** (claim.attempts - 1))
                self._repository.retry(
                    claim,
                    datetime.now(UTC) + delay,
                    failure.stage,
                    failure.code,
                )
            else:
                self._repository.fail(claim, failure.stage, failure.code)
        except DeploymentClaimLostError:
            return
