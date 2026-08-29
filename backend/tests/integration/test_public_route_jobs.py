from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import UUID, uuid4

import pytest
from conftest import FakeGit
from psycopg.errors import CheckViolation

from heimdall.database import Database
from heimdall.projects.repository import PostgresProjectRepository
from heimdall.projects.schemas import ProjectCreate
from heimdall.projects.service import ProjectService
from heimdall.public_routes.models import (
    PublicRouteClaimLostError,
    PublicRouteConflictError,
    PublicRouteStatus,
)
from heimdall.public_routes.repository import PostgresPublicRouteRepository

CONTROL_URL = os.environ.get("HEIMDALL_TEST_CONTROL_DB_URL")

pytestmark = pytest.mark.skipif(
    not CONTROL_URL,
    reason="Control PostgreSQL integration URL is not configured",
)


def test_route_revision_fences_stale_claim_and_preserves_applied_hostname() -> None:
    assert CONTROL_URL is not None
    control = Database(CONTROL_URL)
    control.open()
    project_ids = []
    try:
        projects = ProjectService(PostgresProjectRepository(control), FakeGit())
        routes = PostgresPublicRouteRepository(control)
        run_id = uuid4().hex
        first_project = projects.create(
            ProjectCreate(
                name=f"Public-A-{run_id}",
                repositoryUrl=f"https://github.com/example/public-a-{run_id}",
            )
        )
        project_ids.append(first_project.id)
        second_project = projects.create(
            ProjectCreate(
                name=f"Public-B-{run_id}",
                repositoryUrl=f"https://github.com/example/public-b-{run_id}",
            )
        )
        project_ids.append(second_project.id)

        created = routes.set_enabled(
            first_project.id,
            f"student-{run_id[:8]}",
            f"student-{run_id[:8]}.deployments.test",
        )
        first_claim = routes.claim_next("routing-one", timedelta(seconds=1))
        assert first_claim is not None

        unchanged = routes.set_enabled(first_project.id, created.subdomain, created.hostname)
        assert unchanged.desired_revision == 1
        assert unchanged.status is PublicRouteStatus.APPLYING
        routes.renew(first_claim, timedelta(seconds=1))
        active = routes.complete(first_claim)
        assert active.status is PublicRouteStatus.ACTIVE
        assert active.applied_hostname == created.hostname

        renamed_hostname = f"renamed-{run_id[:8]}.deployments.test"
        renamed = routes.set_enabled(
            first_project.id,
            f"renamed-{run_id[:8]}",
            renamed_hostname,
        )
        assert renamed.desired_revision == 2
        assert renamed.applied_hostname == created.hostname
        assert {item.project_id: item.applied_hostname for item in routes.list_applied()}[
            first_project.id
        ] == created.hostname

        with pytest.raises(PublicRouteConflictError):
            routes.set_enabled(
                second_project.id,
                created.subdomain,
                created.hostname,
            )

        stale = routes.claim_next("routing-stale", timedelta(seconds=1))
        assert stale is not None
        updated = routes.set_enabled(
            first_project.id,
            f"latest-{run_id[:8]}",
            f"latest-{run_id[:8]}.deployments.test",
        )
        assert updated.desired_revision == 3
        with pytest.raises(PublicRouteClaimLostError):
            routes.complete(stale)

        latest = routes.claim_next("routing-latest", timedelta(seconds=1))
        assert latest is not None
        failed = routes.fail(latest, "EDGE_RELOAD_FAILED", uncertain=False)
        assert failed.status is PublicRouteStatus.FAILED
        assert failed.applied_hostname == created.hostname

        explicit_retry = routes.set_enabled(
            first_project.id,
            updated.subdomain,
            updated.hostname,
        )
        assert explicit_retry.desired_revision == 3
        final_claim = routes.claim_next("routing-final", timedelta(seconds=1))
        assert final_claim is not None
        final = routes.complete(final_claim)
        assert final.status is PublicRouteStatus.ACTIVE
        assert final.applied_hostname == updated.hostname

        disabling = routes.disable(first_project.id)
        assert disabling.desired_revision == 4
        disable_claim = routes.claim_next("routing-disable", timedelta(seconds=1))
        assert disable_claim is not None
        unchanged_disable = routes.disable(first_project.id)
        assert unchanged_disable.status is PublicRouteStatus.APPLYING
        routes.renew(disable_claim, timedelta(seconds=1))
        inactive = routes.complete(disable_claim)
        assert inactive.status is PublicRouteStatus.INACTIVE
        assert inactive.applied_hostname is None
    finally:
        _delete_projects(control, project_ids)
        control.close()


def test_expired_route_claim_is_recovered_with_a_new_token() -> None:
    assert CONTROL_URL is not None
    control = Database(CONTROL_URL)
    control.open()
    project_ids = []
    try:
        projects = ProjectService(PostgresProjectRepository(control), FakeGit())
        routes = PostgresPublicRouteRepository(control)
        run_id = uuid4().hex
        project = projects.create(
            ProjectCreate(
                name=f"Public-lease-{run_id}",
                repositoryUrl=f"https://github.com/example/public-lease-{run_id}",
            )
        )
        project_ids.append(project.id)
        routes.set_enabled(
            project.id,
            f"lease-{run_id[:8]}",
            f"lease-{run_id[:8]}.deployments.test",
        )

        first = routes.claim_next("routing-one", timedelta(milliseconds=50))
        assert first is not None
        recovered = None
        deadline = time.monotonic() + 1
        while recovered is None and time.monotonic() < deadline:
            recovered = routes.claim_next("routing-two", timedelta(seconds=1))
            if recovered is None:
                time.sleep(0.01)

        assert recovered is not None
        assert recovered.token != first.token
        with pytest.raises(PublicRouteClaimLostError):
            routes.fail(first, "STALE", uncertain=False)
        routes.fail(recovered, "TEST_TERMINAL", uncertain=False)
    finally:
        _delete_projects(control, project_ids)
        control.close()


def test_claim_cannot_complete_after_expiring_while_waiting_for_global_lock() -> None:
    assert CONTROL_URL is not None
    control = Database(CONTROL_URL)
    control.open()
    project_ids = []
    try:
        projects = ProjectService(PostgresProjectRepository(control), FakeGit())
        routes = PostgresPublicRouteRepository(control)
        run_id = uuid4().hex
        project = projects.create(
            ProjectCreate(
                name=f"Public-lock-{run_id}",
                repositoryUrl=f"https://github.com/example/public-lock-{run_id}",
            )
        )
        project_ids.append(project.id)
        routes.set_enabled(
            project.id,
            f"lock-{run_id[:8]}",
            f"lock-{run_id[:8]}.deployments.test",
        )
        claim = routes.claim_next("routing-lock", timedelta(milliseconds=100))
        assert claim is not None
        started = Event()

        def complete_after_lock() -> None:
            started.set()
            routes.complete(claim)

        with ThreadPoolExecutor(max_workers=1) as executor:
            with control.connection() as blocker:
                blocker.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    ("heimdall-public-hostname-claims",),
                )
                future = executor.submit(complete_after_lock)
                assert started.wait(timeout=1)
                time.sleep(0.2)
            with pytest.raises(PublicRouteClaimLostError):
                future.result(timeout=1)

        recovered = routes.claim_next("routing-recovered", timedelta(seconds=1))
        assert recovered is not None
        routes.fail(recovered, "TEST_TERMINAL", uncertain=False)
    finally:
        _delete_projects(control, project_ids)
        control.close()


def test_active_route_requires_a_complete_applied_snapshot() -> None:
    assert CONTROL_URL is not None
    control = Database(CONTROL_URL)
    control.open()
    project_ids = []
    try:
        projects = ProjectService(PostgresProjectRepository(control), FakeGit())
        routes = PostgresPublicRouteRepository(control)
        run_id = uuid4().hex
        project = projects.create(
            ProjectCreate(
                name=f"Public-check-{run_id}",
                repositoryUrl=f"https://github.com/example/public-check-{run_id}",
            )
        )
        project_ids.append(project.id)
        routes.set_enabled(
            project.id,
            f"check-{run_id[:8]}",
            f"check-{run_id[:8]}.deployments.test",
        )

        with pytest.raises(CheckViolation), control.connection() as connection:
            connection.execute(
                """
                UPDATE project_public_routes
                SET status = 'ACTIVE', applied_revision = NULL,
                    applied_hostname = NULL
                WHERE project_id = %s
                """,
                (project.id,),
            )
    finally:
        _delete_projects(control, project_ids)
        control.close()


def test_runtime_success_wakes_only_the_current_gateway_wait_job() -> None:
    assert CONTROL_URL is not None
    control = Database(CONTROL_URL)
    control.open()
    project_ids = []
    try:
        projects = ProjectService(PostgresProjectRepository(control), FakeGit())
        routes = PostgresPublicRouteRepository(control)
        run_id = uuid4().hex
        target = projects.create(
            ProjectCreate(
                name=f"Public-wake-target-{run_id}",
                repositoryUrl=f"https://github.com/example/public-wake-target-{run_id}",
            )
        )
        project_ids.append(target.id)
        other = projects.create(
            ProjectCreate(
                name=f"Public-wake-other-{run_id}",
                repositoryUrl=f"https://github.com/example/public-wake-other-{run_id}",
            )
        )
        project_ids.append(other.id)
        routes.set_enabled(
            target.id,
            f"wake-target-{run_id[:8]}",
            f"wake-target-{run_id[:8]}.deployments.test",
        )
        routes.set_enabled(
            other.id,
            f"wake-other-{run_id[:8]}",
            f"wake-other-{run_id[:8]}.deployments.test",
        )

        with control.connection() as connection:
            for project_id in project_ids:
                connection.execute(
                    """
                    UPDATE project_public_routes
                    SET last_error_code = 'GATEWAY_START_FAILED'
                    WHERE project_id = %s
                    """,
                    (project_id,),
                )
                connection.execute(
                    """
                    UPDATE public_route_jobs
                    SET available_at = clock_timestamp() + interval '1 hour',
                        last_error_code = 'GATEWAY_START_FAILED'
                    WHERE project_id = %s
                    """,
                    (project_id,),
                )

        routes.wake_pending(target.id)

        with control.connection() as connection:
            rows = connection.execute(
                """
                SELECT project_id, available_at, clock_timestamp() AS observed_at
                FROM public_route_jobs
                WHERE project_id IN (%s, %s)
                """,
                (target.id, other.id),
            ).fetchall()
        by_project = {row["project_id"]: row for row in rows}
        assert by_project[target.id]["available_at"] <= by_project[target.id]["observed_at"]
        assert by_project[other.id]["available_at"] > by_project[other.id]["observed_at"]

        with control.connection() as connection:
            connection.execute(
                """
                UPDATE public_route_jobs
                SET desired_revision = desired_revision + 1,
                    available_at = clock_timestamp() + interval '1 hour'
                WHERE project_id = %s
                """,
                (target.id,),
            )
        routes.wake_pending(target.id)
        with control.connection() as connection:
            stale = connection.execute(
                """
                SELECT available_at, clock_timestamp() AS observed_at
                FROM public_route_jobs
                WHERE project_id = %s
                """,
                (target.id,),
            ).fetchone()
        assert stale["available_at"] > stale["observed_at"]
    finally:
        _delete_projects(control, project_ids)
        control.close()


def _delete_projects(control: Database, project_ids: list[UUID]) -> None:
    if not project_ids:
        return
    with control.connection() as connection:
        for project_id in project_ids:
            connection.execute("DELETE FROM projects WHERE id = %s", (project_id,))
