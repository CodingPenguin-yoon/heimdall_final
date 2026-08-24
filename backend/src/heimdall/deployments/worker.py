from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from heimdall.deployments.diagnostics import DiagnosticArtifactDraft, FailedCommandOutput
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
    command_output: FailedCommandOutput | None = field(default=None, repr=False)


class RecoveryDisposition(StrEnum):
    ACTIVE = "ACTIVE"
    SAFE_TO_RETRY = "SAFE_TO_RETRY"
    UNCERTAIN = "UNCERTAIN"


class RuntimeProcessor(Protocol):
    def recover(self, deployment: Deployment, progress: RuntimeProgress) -> RecoveryDisposition: ...

    def process(self, deployment: Deployment, progress: RuntimeProgress) -> None: ...

    def cleanup_candidate(self, deployment: Deployment) -> None: ...

    def rollback_candidate(self, deployment: Deployment) -> None: ...

    def capture_diagnostics(
        self, deployment: Deployment, failure: RuntimeFailure, progress: RuntimeProgress
    ) -> tuple[DiagnosticArtifactDraft, ...]: ...


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
        diagnostic_retention: timedelta | None = None,
        on_runtime_ready: Callable[[UUID], None] | None = None,
    ) -> None:
        if not worker_id or len(worker_id) > 128:
            raise ValueError("worker_id must be between 1 and 128 characters")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if diagnostic_retention is not None and diagnostic_retention <= timedelta(0):
            raise ValueError("diagnostic_retention must be positive")
        self._repository = repository
        self._processor = processor
        self._worker_id = worker_id
        self._lease_duration = lease_duration
        self._max_attempts = max_attempts
        self._retry_base_delay = retry_base_delay
        self._diagnostic_retention = diagnostic_retention
        self._on_runtime_ready = on_runtime_ready

    def run_once(self) -> bool:
        claim = self._repository.claim_next(self._worker_id, self._lease_duration)
        if claim is None:
            if self._diagnostic_retention is not None:
                with suppress(Exception):
                    self._repository.purge_expired_diagnostics(limit=100)
            return False
        progress = RuntimeProgress(self._repository, claim, self._lease_duration)
        try:
            if claim.attempts > 1:
                recovery = self._processor.recover(claim.deployment, progress)
                if recovery is RecoveryDisposition.ACTIVE:
                    progress.heartbeat()
                    self._succeed(claim)
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
            self._succeed(claim)
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

    def _succeed(self, claim: DeploymentJobClaim) -> None:
        deployment = self._repository.succeed(claim)
        if self._on_runtime_ready is not None:
            with suppress(Exception):
                self._on_runtime_ready(deployment.project_id)

    def _handle_failure(self, claim: DeploymentJobClaim, failure: RuntimeFailure) -> None:
        if failure.cleanup_candidate:
            if self._diagnostic_retention is not None:
                rollback_ready = False
                try:
                    self._processor.rollback_candidate(claim.deployment)
                    rollback_ready = True
                except Exception:
                    pass
                if rollback_ready:
                    try:
                        progress = RuntimeProgress(
                            self._repository,
                            claim,
                            self._lease_duration,
                        )
                        artifacts = self._processor.capture_diagnostics(
                            claim.deployment,
                            failure,
                            progress,
                        )
                        self._repository.record_diagnostics(
                            claim,
                            failure_stage=failure.stage,
                            failure_code=failure.code,
                            artifacts=artifacts,
                            retention=self._diagnostic_retention,
                        )
                    except DeploymentClaimLostError:
                        return
                    except Exception:
                        pass
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
