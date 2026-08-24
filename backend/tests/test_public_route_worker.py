from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from heimdall.deployments.worker import RuntimeFailure
from heimdall.public_routes.models import (
    PublicRoute,
    PublicRouteClaimLostError,
    PublicRouteDesiredState,
    PublicRouteJobClaim,
    PublicRouteStatus,
)
from heimdall.public_routes.worker import PublicRouteWorker
from heimdall.runtime.edge import EdgeFinalizeRejectedError, EdgeRuntimeError


def public_route(
    *,
    desired_state: PublicRouteDesiredState = PublicRouteDesiredState.ENABLED,
    hostname: str = "student.deployments.test",
    applied_hostname: str | None = None,
) -> PublicRoute:
    now = datetime.now(UTC)
    return PublicRoute(
        project_id=uuid4(),
        subdomain=hostname.split(".", 1)[0],
        hostname=hostname,
        desired_state=desired_state,
        status=PublicRouteStatus.APPLYING,
        desired_revision=2,
        applied_revision=1 if applied_hostname is not None else None,
        applied_hostname=applied_hostname,
        last_error_code=None,
        created_at=now,
        updated_at=now,
    )


class Routes:
    def __init__(self, claim: PublicRouteJobClaim, applied=()) -> None:
        self.claim = claim
        self.available = True
        self.applied = list(applied)
        self.completed = False
        self.deferred: str | None = None
        self.retried: str | None = None
        self.failed: tuple[str, bool] | None = None
        self.heartbeats = 0
        self.change_snapshot_on_fence = False
        self.snapshot_reads = 0
        self.claim_calls = 0

    def claim_next(self, worker_id, lease_duration):
        self.claim_calls += 1
        if not self.available:
            return None
        self.available = False
        return self.claim

    def list_applied(self):
        self.snapshot_reads += 1
        if self.change_snapshot_on_fence and self.snapshot_reads > 1:
            changed = public_route(
                hostname="concurrent.deployments.test",
                applied_hostname="concurrent.deployments.test",
            )
            return [*self.applied, changed]
        return list(self.applied)

    def renew(self, claim, lease_duration):
        self.heartbeats += 1
        return datetime.now(UTC) + lease_duration

    def complete(self, claim):
        self.completed = True
        return claim.route

    def defer_not_ready(self, claim, available_at, code):
        self.deferred = code
        return claim.route

    def retry(self, claim, available_at, code):
        self.retried = code
        return claim.route

    def fail(self, claim, code, *, uncertain):
        self.failed = (code, uncertain)
        return claim.route


class EdgeNetwork:
    def __init__(self, failure: RuntimeFailure | None = None) -> None:
        self.failure = failure
        self.projects = []

    def ensure_gateway_attached(self, project_id, *, heartbeat):
        heartbeat()
        self.projects.append(project_id)
        if self.failure is not None:
            raise self.failure


class EdgeConfig:
    def __init__(self, failure: EdgeRuntimeError | None = None) -> None:
        self.failure = failure
        self.targets = []
        self.change = None
        self.probe_all_routes = False
        self.calls = []
        self.reconciliation_failures = 0

    def apply(
        self,
        routes,
        change,
        *,
        heartbeat,
        fence,
        finalize,
        probe_all_routes=False,
    ):
        self.calls.append((list(routes), change))
        self.targets = list(routes)
        self.change = change
        self.probe_all_routes = probe_all_routes
        heartbeat()
        fence()
        if self.failure is not None:
            raise self.failure
        if change is None and self.reconciliation_failures > 0:
            self.reconciliation_failures -= 1
            raise EdgeRuntimeError("EDGE_RECONCILIATION_TEST_FAILED", retryable=True)
        try:
            finalize()
        except EdgeFinalizeRejectedError:
            raise
        except Exception as error:
            raise EdgeRuntimeError("EDGE_FINALIZE_UNCERTAIN", uncertain=True) from error


def claim(route: PublicRoute, *, attempts: int = 1) -> PublicRouteJobClaim:
    return PublicRouteJobClaim(
        route=route,
        token=uuid4(),
        worker_id="routing-worker",
        desired_revision=route.desired_revision,
        attempts=attempts,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )


def worker(repository, network, config, *, max_attempts=3):
    return PublicRouteWorker(
        repository,
        network,
        config,
        worker_id="routing-worker",
        lease_duration=timedelta(minutes=1),
        max_attempts=max_attempts,
    )


def test_enabled_route_replaces_only_its_applied_snapshot_and_completes() -> None:
    desired = public_route(applied_hostname="old.deployments.test")
    other = public_route(
        hostname="other.deployments.test", applied_hostname="other.deployments.test"
    )
    repository = Routes(claim(desired), [replace(desired), other])
    network = EdgeNetwork()
    config = EdgeConfig()

    assert worker(repository, network, config).run_once() is True

    by_project = {item.project_id: item for item in config.targets}
    assert by_project[desired.project_id].hostname == desired.hostname
    assert by_project[other.project_id].hostname == other.applied_hostname
    assert config.change.hostname == desired.hostname
    assert config.change.previous_hostname == "old.deployments.test"
    assert repository.completed is True
    assert network.projects == [desired.project_id]


def test_disable_removes_route_and_probes_the_previous_applied_hostname() -> None:
    desired = public_route(
        desired_state=PublicRouteDesiredState.DISABLED,
        hostname="new.deployments.test",
        applied_hostname="old.deployments.test",
    )
    repository = Routes(claim(desired), [desired])
    config = EdgeConfig()

    worker(repository, EdgeNetwork(), config).run_once()

    assert config.targets == []
    assert config.change.enabled is False
    assert config.change.hostname == "old.deployments.test"
    assert repository.completed is True


def test_missing_project_gateway_stays_pending_beyond_attempt_limit() -> None:
    desired = public_route()
    repository = Routes(claim(desired, attempts=7))
    network = EdgeNetwork(RuntimeFailure("ACTIVATION", "GATEWAY_START_FAILED", retryable=True))

    worker(repository, network, EdgeConfig(), max_attempts=3).run_once()

    assert repository.deferred == "GATEWAY_START_FAILED"
    assert repository.failed is None


def test_uncertain_restore_failure_is_terminal_uncertain() -> None:
    desired = public_route()
    repository = Routes(claim(desired))
    config = EdgeConfig(EdgeRuntimeError("EDGE_STATE_UNCERTAIN", uncertain=True))

    worker(repository, EdgeNetwork(), config).run_once()

    assert repository.failed == ("EDGE_STATE_UNCERTAIN", True)


def test_startup_reconciliation_uses_only_durable_applied_hostnames() -> None:
    desired = public_route(applied_hostname="old.deployments.test")
    repository = Routes(claim(desired), [desired])
    network = EdgeNetwork()
    config = EdgeConfig()

    assert worker(repository, network, config).reconcile_startup() is True

    assert [item.hostname for item in config.targets] == ["old.deployments.test"]
    assert config.probe_all_routes is True
    assert network.projects == [desired.project_id]


def test_changed_applied_snapshot_requeues_claim_before_config_finalize() -> None:
    desired = public_route()
    repository = Routes(claim(desired))
    repository.change_snapshot_on_fence = True

    worker(repository, EdgeNetwork(), EdgeConfig()).run_once()

    assert repository.retried == "ROUTING_SNAPSHOT_CHANGED"
    assert repository.completed is False


def test_startup_reconciliation_rejects_a_stale_applied_snapshot() -> None:
    desired = public_route(applied_hostname="old.deployments.test")
    repository = Routes(claim(desired), [desired])
    repository.change_snapshot_on_fence = True

    assert worker(repository, EdgeNetwork(), EdgeConfig()).reconcile_startup() is False


def test_definite_claim_loss_is_translated_to_a_rejected_finalize() -> None:
    class StaleRoutes(Routes):
        def complete(self, claim):
            raise PublicRouteClaimLostError

    desired = public_route(applied_hostname="old.deployments.test")
    repository = StaleRoutes(claim(desired), [desired])

    assert worker(repository, EdgeNetwork(), EdgeConfig()).run_once() is True

    assert repository.failed is None
    assert repository.retried is None


def test_precommit_finalize_failure_records_uncertain_then_reconciles_previous() -> None:
    class PrecommitFailureRoutes(Routes):
        def complete(self, claim):
            raise RuntimeError("transaction failed before commit")

    desired = public_route(applied_hostname="old.deployments.test")
    repository = PrecommitFailureRoutes(claim(desired), [desired])
    config = EdgeConfig()
    route_worker = worker(repository, EdgeNetwork(), config)

    assert route_worker.run_once() is True
    assert repository.failed == ("EDGE_FINALIZE_UNCERTAIN", True)

    assert route_worker.run_once() is False
    assert config.calls[-1][1] is None
    assert [target.hostname for target in config.calls[-1][0]] == ["old.deployments.test"]


def test_commit_ack_error_requires_successful_reconciliation_before_next_claim() -> None:
    first_route = public_route(applied_hostname="old.deployments.test")
    second_route = public_route(hostname="second.deployments.test")
    first_claim = claim(first_route)
    second_claim = claim(second_route)

    class CommitAckRoutes(Routes):
        def __init__(self) -> None:
            super().__init__(first_claim, [first_route])
            self.next_claim = first_claim
            self.ack_error_pending = True
            self.committed_claim = None

        def claim_next(self, worker_id, lease_duration):
            self.claim_calls += 1
            claimed = self.next_claim
            self.next_claim = None
            return claimed

        def complete(self, claimed):
            if self.ack_error_pending:
                self.ack_error_pending = False
                self.committed_claim = claimed
                self.applied = [
                    replace(
                        claimed.route,
                        status=PublicRouteStatus.ACTIVE,
                        applied_revision=claimed.desired_revision,
                        applied_hostname=claimed.route.hostname,
                    )
                ]
                raise RuntimeError("commit succeeded but acknowledgement was lost")
            self.completed = True
            return claimed.route

        def fail(self, claimed, code, *, uncertain):
            if claimed is self.committed_claim:
                raise PublicRouteClaimLostError
            return super().fail(claimed, code, uncertain=uncertain)

    repository = CommitAckRoutes()
    config = EdgeConfig()
    route_worker = worker(repository, EdgeNetwork(), config)

    assert route_worker.run_once() is True
    assert repository.failed is None

    repository.next_claim = second_claim
    config.reconciliation_failures = 1
    assert route_worker.run_once() is False
    assert repository.claim_calls == 1
    assert config.calls[-1][1] is None

    assert route_worker.run_once() is True
    assert repository.claim_calls == 2
    assert [call[1] is None for call in config.calls[-3:]] == [True, True, False]
