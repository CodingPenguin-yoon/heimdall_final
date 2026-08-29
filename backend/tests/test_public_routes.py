from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from heimdall.common.errors import AppError, install_error_handlers
from heimdall.public_routes.models import (
    PublicRoute,
    PublicRouteConflictError,
    PublicRouteDesiredState,
    PublicRouteNotFoundError,
    PublicRouteStatus,
)
from heimdall.public_routes.router import router
from heimdall.public_routes.schemas import PublicRouteUpdate
from heimdall.public_routes.service import PublicRouteService


class Projects:
    def __init__(self, project_id) -> None:
        self.project_id = project_id

    def get(self, project_id):
        if project_id != self.project_id:
            raise AssertionError("unexpected project")
        return object()


class Routes:
    def __init__(self, project_id) -> None:
        self.project_id = project_id
        self.item: PublicRoute | None = None
        self.conflict = False
        self.woken_project_id = None

    def get(self, project_id):
        if self.item is None:
            raise PublicRouteNotFoundError
        return self.item

    def set_enabled(self, project_id, subdomain, hostname):
        if self.conflict:
            raise PublicRouteConflictError
        now = datetime.now(UTC)
        revision = 1 if self.item is None else self.item.desired_revision + 1
        self.item = PublicRoute(
            project_id=project_id,
            subdomain=subdomain,
            hostname=hostname,
            desired_state=PublicRouteDesiredState.ENABLED,
            status=PublicRouteStatus.PENDING,
            desired_revision=revision,
            applied_revision=None,
            applied_hostname=None,
            last_error_code=None,
            created_at=self.item.created_at if self.item is not None else now,
            updated_at=now,
        )
        return self.item

    def disable(self, project_id):
        if self.item is None:
            raise PublicRouteNotFoundError
        self.item = replace(
            self.item,
            desired_state=PublicRouteDesiredState.DISABLED,
            status=PublicRouteStatus.PENDING,
            desired_revision=self.item.desired_revision + 1,
            updated_at=datetime.now(UTC),
        )
        return self.item

    def wake_pending(self, project_id):
        self.woken_project_id = project_id


def public_routes(project_id=None):
    resolved = project_id or uuid4()
    repository = Routes(resolved)
    service = PublicRouteService(
        repository,
        Projects(resolved),
        "deployments.example.test",
        ("admin", "api", "www"),
    )
    return resolved, repository, service


def test_service_uses_only_normalized_subdomain_to_derive_hostname() -> None:
    project_id, repository, service = public_routes()

    route = service.set(project_id, PublicRouteUpdate.model_validate({"subdomain": " Student-A "}))

    assert route.subdomain == "student-a"
    assert route.hostname == "student-a.deployments.example.test"
    assert repository.item == route


@pytest.mark.parametrize("subdomain", ["-student", "student-", "student--a", "student_a", "학생"])
def test_schema_rejects_noncanonical_public_subdomain(subdomain: str) -> None:
    with pytest.raises(ValueError):
        PublicRouteUpdate(subdomain=subdomain)


def test_service_rejects_reserved_label_and_maps_conflict() -> None:
    project_id, repository, service = public_routes()

    with pytest.raises(AppError) as reserved:
        service.set(project_id, PublicRouteUpdate(subdomain="admin"))

    repository.conflict = True
    with pytest.raises(AppError) as conflict:
        service.set(project_id, PublicRouteUpdate(subdomain="student-a"))

    assert reserved.value.code == "PUBLIC_SUBDOMAIN_RESERVED"
    assert conflict.value.code == "PUBLIC_HOSTNAME_CONFLICT"


def test_service_wakes_the_exact_project_after_runtime_success() -> None:
    project_id, repository, service = public_routes()

    service.wake_pending_for_runtime(project_id)

    assert repository.woken_project_id == project_id


def test_router_returns_server_derived_route_and_disable_body() -> None:
    project_id, _, service = public_routes()
    app = FastAPI()
    install_error_handlers(app)
    app.state.public_routes = service
    app.include_router(router, prefix="/api/projects")
    client = TestClient(app)

    missing = client.get(f"/api/projects/{project_id}/public-route")
    created = client.put(
        f"/api/projects/{project_id}/public-route",
        json={"subdomain": "Student-A"},
    )
    client_derived = client.put(
        f"/api/projects/{project_id}/public-route",
        json={"subdomain": "student-a", "hostname": "attacker.example.test"},
    )
    disabled = client.delete(f"/api/projects/{project_id}/public-route")

    assert missing.status_code == 404
    assert missing.json()["code"] == "PUBLIC_ROUTE_NOT_FOUND"
    assert created.status_code == 202
    assert created.json()["hostname"] == "student-a.deployments.example.test"
    assert created.json()["status"] == "PENDING"
    assert client_derived.status_code == 422
    assert disabled.status_code == 202
    assert disabled.json()["desiredState"] == "DISABLED"
    assert disabled.json()["desiredRevision"] == 2
