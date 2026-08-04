from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from heimdall.deployments.models import (
    Deployment,
    DeploymentClaimLostError,
    DeploymentEvent,
    DeploymentJobClaim,
    DeploymentSource,
    DeploymentStatus,
)
from heimdall.deployments.worker import (
    DeploymentWorker,
    RuntimeFailure,
    RuntimeProgress,
)


def deployment() -> Deployment:
    now = datetime.now(UTC)
    return Deployment(
        id=uuid4(),
        project_id=uuid4(),
        source_type=DeploymentSource.MAIN_HEAD,
        requested_commit_sha=None,
        resolved_commit_sha="a" * 40,
        config_version=1,
        config_snapshot={"services": [], "routes": []},
        status=DeploymentStatus.PREPARING,
        failure_stage=None,
        failure_code=None,
        created_at=now,
        updated_at=now,
        terminal_at=None,
    )


class MemoryJobRepository:
    def __init__(self, item: Deployment, *, attempts: int = 1) -> None:
        self.item = item
        self.claim = DeploymentJobClaim(
            deployment=item,
            token=uuid4(),
            worker_id="worker-one",
            attempts=attempts,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        )
        self.claim_available = True
        self.renewals = 0
        self.transitions: list[DeploymentStatus] = []
        self.retry_at: datetime | None = None
        self.completed: str | None = None

    def claim_next(self, worker_id: str, lease_duration: timedelta) -> DeploymentJobClaim | None:
        if not self.claim_available:
            return None
        self.claim_available = False
        assert worker_id == self.claim.worker_id
        return self.claim

    def renew(self, claim: DeploymentJobClaim, lease_duration: timedelta) -> datetime:
        assert claim == self.claim
        self.renewals += 1
        return datetime.now(UTC) + lease_duration

    def advance(
        self,
        claim: DeploymentJobClaim,
        status: DeploymentStatus,
        *,
        event_code: str,
        event_message: str,
    ) -> Deployment:
        assert claim == self.claim
        assert event_code and event_message
        self.transitions.append(status)
        self.item = replace(self.item, status=status)
        return self.item

    def succeed(self, claim: DeploymentJobClaim) -> Deployment:
        assert claim == self.claim
        self.completed = "SUCCEEDED"
        self.item = replace(self.item, status=DeploymentStatus.SUCCEEDED)
        return self.item

    def fail(self, claim: DeploymentJobClaim, stage: str, code: str) -> Deployment:
        assert claim == self.claim
        self.completed = f"FAILED:{stage}:{code}"
        self.item = replace(
            self.item,
            status=DeploymentStatus.FAILED,
            failure_stage=stage,
            failure_code=code,
        )
        return self.item

    def retry(
        self, claim: DeploymentJobClaim, available_at: datetime, stage: str, code: str
    ) -> Deployment:
        assert claim == self.claim
        assert stage and code
        self.completed = "RETRY"
        self.retry_at = available_at
        self.item = replace(self.item, status=DeploymentStatus.QUEUED)
        return self.item

    def create(self, **kwargs):
        raise NotImplementedError

    def list_for_project(self, project_id):
        return []

    def get(self, deployment_id):
        return self.item

    def list_events(self, deployment_id, limit: int = 100) -> list[DeploymentEvent]:
        return []


class SuccessfulProcessor:
    def __init__(self) -> None:
        self.cleaned = False

    def process(self, item: Deployment, progress: RuntimeProgress) -> None:
        progress.stage(DeploymentStatus.BUILDING, "IMAGES_BUILDING", "Building service images")
        progress.stage(DeploymentStatus.STARTING, "SERVICES_STARTING", "Starting services")

    def cleanup_candidate(self, item: Deployment) -> None:
        self.cleaned = True


class FailingProcessor(SuccessfulProcessor):
    def __init__(self, failure: Exception) -> None:
        super().__init__()
        self.failure = failure

    def process(self, item: Deployment, progress: RuntimeProgress) -> None:
        raise self.failure


def worker(repository: MemoryJobRepository, processor) -> DeploymentWorker:
    return DeploymentWorker(
        repository,
        processor,
        worker_id="worker-one",
        lease_duration=timedelta(minutes=1),
        retry_base_delay=timedelta(seconds=1),
    )


def test_worker_advances_stages_and_completes_the_claim() -> None:
    repository = MemoryJobRepository(deployment())
    processor = SuccessfulProcessor()

    assert worker(repository, processor).run_once() is True

    assert repository.transitions == [DeploymentStatus.BUILDING, DeploymentStatus.STARTING]
    assert repository.renewals == 3
    assert repository.completed == "SUCCEEDED"
    assert processor.cleaned is False


def test_retryable_failure_is_scheduled_with_bounded_attempts() -> None:
    repository = MemoryJobRepository(deployment(), attempts=1)
    processor = FailingProcessor(RuntimeFailure("SOURCE", "GIT_TEMPORARILY_UNAVAILABLE", True))

    worker(repository, processor).run_once()

    assert processor.cleaned is True
    assert repository.completed == "RETRY"
    assert repository.retry_at is not None


def test_non_retryable_failure_is_terminal_and_candidate_is_cleaned() -> None:
    repository = MemoryJobRepository(deployment())
    processor = FailingProcessor(RuntimeFailure("BUILD", "IMAGE_BUILD_FAILED"))

    worker(repository, processor).run_once()

    assert processor.cleaned is True
    assert repository.completed == "FAILED:BUILD:IMAGE_BUILD_FAILED"


def test_unknown_exception_uses_a_stable_failure_code() -> None:
    repository = MemoryJobRepository(deployment())
    processor = FailingProcessor(ValueError("must not escape into an event"))

    worker(repository, processor).run_once()

    assert repository.completed == "FAILED:RUNTIME:UNEXPECTED_RUNTIME_FAILURE"


def test_lost_claim_does_not_write_a_terminal_state() -> None:
    repository = MemoryJobRepository(deployment())
    processor = SuccessfulProcessor()

    def lose_claim(claim, lease_duration):
        raise DeploymentClaimLostError

    repository.renew = lose_claim

    assert worker(repository, processor).run_once() is True
    assert repository.completed is None


def test_worker_returns_false_when_no_job_is_available() -> None:
    repository = MemoryJobRepository(deployment())
    repository.claim_available = False

    assert worker(repository, SuccessfulProcessor()).run_once() is False
