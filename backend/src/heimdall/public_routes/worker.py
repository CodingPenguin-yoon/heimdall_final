from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime, timedelta

from heimdall.deployments.worker import RuntimeFailure
from heimdall.public_routes.models import (
    PublicRouteClaimLostError,
    PublicRouteDesiredState,
    PublicRouteJobClaim,
    PublicRouteSnapshotChangedError,
)
from heimdall.public_routes.repository import PublicRouteRepository
from heimdall.runtime.edge import (
    DockerEdgeConfigManager,
    EdgeFinalizeRejectedError,
    EdgeRouteChange,
    EdgeRouteTarget,
    EdgeRuntimeError,
)
from heimdall.runtime.edge_network import EdgeNetworkConnector
from heimdall.runtime.gateway_identity import project_gateway_alias

_GATEWAY_NOT_READY_CODES = {"GATEWAY_START_FAILED"}
_FINALIZE_UNCERTAIN_CODE = "EDGE_FINALIZE_UNCERTAIN"


class RoutingProgress:
    def __init__(
        self,
        repository: PublicRouteRepository,
        claim: PublicRouteJobClaim,
        lease_duration: timedelta,
    ) -> None:
        self._repository = repository
        self._claim = claim
        self._lease_duration = lease_duration

    def heartbeat(self) -> None:
        self._repository.renew(self._claim, self._lease_duration)


class PublicRouteWorker:
    def __init__(
        self,
        repository: PublicRouteRepository,
        edge_network: EdgeNetworkConnector,
        edge_config: DockerEdgeConfigManager,
        *,
        worker_id: str,
        lease_duration: timedelta,
        max_attempts: int = 3,
        retry_base_delay: timedelta = timedelta(seconds=5),
        retry_max_delay: timedelta = timedelta(minutes=1),
    ) -> None:
        if not worker_id or len(worker_id) > 128:
            raise ValueError("worker_id must be between 1 and 128 characters")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if retry_base_delay <= timedelta(0) or retry_max_delay < retry_base_delay:
            raise ValueError("routing retry delays are invalid")
        self._repository = repository
        self._edge_network = edge_network
        self._edge_config = edge_config
        self._worker_id = worker_id
        self._lease_duration = lease_duration
        self._max_attempts = max_attempts
        self._retry_base_delay = retry_base_delay
        self._retry_max_delay = retry_max_delay
        self._reconciliation_required = False

    def reconcile_startup(self) -> bool:
        routes = self._repository.list_applied()
        targets = [_applied_target(route) for route in routes]
        expected_snapshot = _applied_snapshot(routes)

        def fence() -> None:
            if _applied_snapshot(self._repository.list_applied()) != expected_snapshot:
                raise PublicRouteSnapshotChangedError

        try:
            for route in routes:
                self._edge_network.ensure_gateway_attached(
                    route.project_id,
                    heartbeat=lambda: None,
                )
            self._edge_config.apply(
                targets,
                None,
                heartbeat=lambda: None,
                fence=fence,
                finalize=lambda: None,
                probe_all_routes=True,
            )
        except (RuntimeFailure, EdgeRuntimeError, PublicRouteSnapshotChangedError):
            return False
        return True

    def run_once(self) -> bool:
        if self._reconciliation_required:
            if not self.reconcile_startup():
                return False
            self._reconciliation_required = False
        claim = self._repository.claim_next(self._worker_id, self._lease_duration)
        if claim is None:
            return False
        progress = RoutingProgress(self._repository, claim, self._lease_duration)
        try:
            if claim.route.desired_state is PublicRouteDesiredState.ENABLED:
                self._edge_network.ensure_gateway_attached(
                    claim.route.project_id,
                    heartbeat=progress.heartbeat,
                )
            routes = list(self._repository.list_applied())
            expected_snapshot = _applied_snapshot(routes)
            targets = {
                route.project_id: _applied_target(route)
                for route in routes
                if route.applied_hostname is not None
            }
            previous_hostname = claim.route.applied_hostname
            if claim.route.desired_state is PublicRouteDesiredState.ENABLED:
                targets[claim.route.project_id] = EdgeRouteTarget(
                    claim.route.project_id,
                    claim.route.hostname,
                    project_gateway_alias(claim.route.project_id),
                )
                enabled = True
            else:
                targets.pop(claim.route.project_id, None)
                enabled = False
            change = EdgeRouteChange(
                (
                    claim.route.hostname
                    if enabled or previous_hostname is None
                    else previous_hostname
                ),
                enabled,
                previous_hostname=previous_hostname,
            )

            def fence() -> None:
                progress.heartbeat()
                if _applied_snapshot(self._repository.list_applied()) != expected_snapshot:
                    raise PublicRouteSnapshotChangedError

            def finalize() -> None:
                try:
                    self._repository.complete(claim)
                except PublicRouteClaimLostError as error:
                    raise EdgeFinalizeRejectedError from error

            self._edge_config.apply(
                list(targets.values()),
                change,
                heartbeat=progress.heartbeat,
                fence=fence,
                finalize=finalize,
            )
        except EdgeFinalizeRejectedError:
            return True
        except PublicRouteClaimLostError:
            return True
        except PublicRouteSnapshotChangedError:
            with suppress(PublicRouteClaimLostError):
                self._repository.retry(
                    claim,
                    datetime.now(UTC),
                    "ROUTING_SNAPSHOT_CHANGED",
                )
        except RuntimeFailure as failure:
            if failure.code in _GATEWAY_NOT_READY_CODES:
                self._defer_not_ready(claim, failure.code)
            else:
                self._handle_failure(
                    claim,
                    failure.code,
                    retryable=failure.retryable,
                    uncertain=False,
                )
        except EdgeRuntimeError as failure:
            if failure.code == _FINALIZE_UNCERTAIN_CODE:
                self._reconciliation_required = True
            self._handle_failure(
                claim,
                failure.code,
                retryable=failure.retryable,
                uncertain=failure.uncertain,
            )
        except Exception:
            self._handle_failure(
                claim,
                "UNEXPECTED_ROUTING_FAILURE",
                retryable=False,
                uncertain=False,
            )
        return True

    def _defer_not_ready(self, claim: PublicRouteJobClaim, code: str) -> None:
        try:
            self._repository.defer_not_ready(
                claim,
                datetime.now(UTC) + self._retry_delay(claim.attempts),
                code,
            )
        except PublicRouteClaimLostError:
            return

    def _handle_failure(
        self,
        claim: PublicRouteJobClaim,
        code: str,
        *,
        retryable: bool,
        uncertain: bool,
    ) -> None:
        try:
            if uncertain:
                self._repository.fail(claim, code, uncertain=True)
            elif retryable and claim.attempts < self._max_attempts:
                self._repository.retry(
                    claim,
                    datetime.now(UTC) + self._retry_delay(claim.attempts),
                    code,
                )
            else:
                self._repository.fail(claim, code, uncertain=False)
        except PublicRouteClaimLostError:
            return

    def _retry_delay(self, attempts: int) -> timedelta:
        multiplier = 2 ** min(max(attempts - 1, 0), 16)
        return min(self._retry_base_delay * multiplier, self._retry_max_delay)


def _applied_target(route) -> EdgeRouteTarget:
    if route.applied_hostname is None:
        raise ValueError("applied route snapshot is missing its hostname")
    return EdgeRouteTarget(
        route.project_id,
        route.applied_hostname,
        project_gateway_alias(route.project_id),
    )


def _applied_snapshot(routes) -> tuple[tuple[str, int | None, str], ...]:
    return tuple(
        sorted(
            (
                str(route.project_id),
                route.applied_revision,
                route.applied_hostname,
            )
            for route in routes
            if route.applied_hostname is not None
        )
    )
