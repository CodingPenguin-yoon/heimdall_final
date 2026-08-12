from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_runtime_models import runtime_deployment

from heimdall.common.errors import install_error_handlers
from heimdall.deployments.diagnostics import (
    DeploymentDiagnosticArtifact,
    DiagnosticArtifactKind,
    DiagnosticCaptureStatus,
    DiagnosticLine,
    DiagnosticStream,
)
from heimdall.deployments.event_stream import (
    DeploymentEventStreamEnd,
    DeploymentEventStreamReady,
)
from heimdall.deployments.models import DeploymentEvent
from heimdall.deployments.router import router
from heimdall.runtime.logs import (
    ServiceLogLine,
    ServiceLogSnapshot,
    ServiceLogStream,
    ServiceLogStreamEnd,
    ServiceLogStreamLine,
    ServiceLogStreamReady,
)


class LogSubscription:
    def __init__(self, deployment_id) -> None:
        self.ready = ServiceLogStreamReady(
            deployment_id,
            ("web", "api"),
            "api",
            datetime(2026, 8, 10, 1, tzinfo=UTC),
        )
        self.events = [
            None,
            ServiceLogStreamLine(
                ServiceLogLine(
                    "2026-08-10T01:00:01.000000000Z",
                    ServiceLogStream.STDERR,
                    "warning",
                ),
                False,
            ),
            ServiceLogStreamEnd(),
        ]
        self.closed = False

    def receive(self):
        return self.events.pop(0)

    def close(self) -> None:
        self.closed = True


class EventSubscription:
    def __init__(self, deployment_id, after_id: int) -> None:
        self.ready = DeploymentEventStreamReady(deployment_id, after_id)
        self.events = [
            None,
            DeploymentEvent(
                id=after_id + 1,
                deployment_id=deployment_id,
                stage="BUILDING",
                code="IMAGES_BUILDING",
                message="Building service images",
                created_at=datetime(2026, 8, 10, 1, tzinfo=UTC),
            ),
            DeploymentEventStreamEnd(),
        ]
        self.closed = False

    def receive(self):
        return self.events.pop(0)

    def close(self) -> None:
        self.closed = True


class DeploymentCatalog:
    def __init__(self) -> None:
        self.item = runtime_deployment()
        self.diagnostic_item = DeploymentDiagnosticArtifact(
            id=uuid4(),
            deployment_id=self.item.id,
            event_id=42,
            kind=DiagnosticArtifactKind.COMMAND_OUTPUT,
            failure_stage="BUILD",
            failure_code="IMAGE_BUILD_FAILED",
            capture_status=DiagnosticCaptureStatus.CAPTURED,
            capture_code=None,
            operation="DOCKER_BUILD",
            service_name=None,
            return_code=17,
            container_status=None,
            container_exit_code=None,
            line_count=1,
            byte_count=96,
            truncated=False,
            captured_at=datetime(2026, 8, 7, 1, tzinfo=UTC),
            expires_at=datetime(2026, 9, 6, 1, tzinfo=UTC),
            lines=(DiagnosticLine(DiagnosticStream.STDERR, "build failed"),),
        )

    def list_recent(self):
        return [self.item]

    def service_logs(self, deployment_id, service_name):
        return ServiceLogSnapshot(
            deployment_id=deployment_id,
            services=("web", "api"),
            service_name=service_name or "web",
            retrieved_at=datetime(2026, 8, 7, 1, tzinfo=UTC),
            lines=(
                ServiceLogLine(
                    "2026-08-07T01:00:00.000000000Z",
                    ServiceLogStream.STDOUT,
                    "ready",
                ),
            ),
            truncated=False,
        )

    def open_service_log_stream(self, deployment_id, service_name):
        assert service_name == "api"
        self.subscription = LogSubscription(deployment_id)
        return self.subscription

    def open_event_stream(self, deployment_id, after_id):
        self.event_subscription = EventSubscription(deployment_id, after_id)
        return self.event_subscription

    def diagnostics(self, deployment_id):
        assert deployment_id == self.item.id
        return [self.diagnostic_item]

    def diagnostic(self, deployment_id, artifact_id):
        assert deployment_id == self.item.id
        assert artifact_id == self.diagnostic_item.id
        return self.diagnostic_item


def test_global_deployments_returns_existing_public_dto() -> None:
    app = FastAPI()
    catalog = DeploymentCatalog()
    app.state.deployments = catalog
    app.include_router(router, prefix="/api")

    response = TestClient(app).get("/api/deployments")

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "id": str(catalog.item.id),
                "projectId": str(catalog.item.project_id),
                "sourceType": catalog.item.source_type.value,
                "requestedCommitSha": catalog.item.requested_commit_sha,
                "resolvedCommitSha": catalog.item.resolved_commit_sha,
                "configVersion": catalog.item.config_version,
                "status": catalog.item.status.value,
                "failureStage": catalog.item.failure_stage,
                "failureCode": catalog.item.failure_code,
                "createdAt": catalog.item.created_at.isoformat().replace("+00:00", "Z"),
                "updatedAt": catalog.item.updated_at.isoformat().replace("+00:00", "Z"),
                "terminalAt": None,
            }
        ]
    }
    assert "configSnapshot" not in response.text


def test_service_logs_return_bounded_public_contract_without_caching() -> None:
    app = FastAPI()
    catalog = DeploymentCatalog()
    app.state.deployments = catalog
    app.include_router(router, prefix="/api")

    response = TestClient(app).get(f"/api/deployments/{catalog.item.id}/service-logs?service=api")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "deploymentId": str(catalog.item.id),
        "services": ["web", "api"],
        "serviceName": "api",
        "retrievedAt": "2026-08-07T01:00:00Z",
        "lines": [
            {
                "timestamp": "2026-08-07T01:00:00.000000000Z",
                "stream": "STDOUT",
                "message": "ready",
            }
        ],
        "truncated": False,
    }


def test_diagnostic_list_omits_payload_and_detail_returns_bounded_lines() -> None:
    app = FastAPI()
    catalog = DeploymentCatalog()
    app.state.deployments = catalog
    app.include_router(router, prefix="/api")
    client = TestClient(app)

    listed = client.get(f"/api/deployments/{catalog.item.id}/diagnostics")
    detail = client.get(
        f"/api/deployments/{catalog.item.id}/diagnostics/{catalog.diagnostic_item.id}"
    )

    assert listed.status_code == 200
    assert listed.headers["cache-control"] == "no-store"
    assert "lines" not in listed.json()["items"][0]
    assert listed.json()["items"][0]["eventId"] == 42
    assert detail.status_code == 200
    assert detail.headers["cache-control"] == "no-store"
    assert detail.json()["lines"] == [
        {"timestamp": None, "stream": "STDERR", "message": "build failed"}
    ]


def test_service_logs_reject_noncanonical_service_name_before_the_service_layer() -> None:
    app = FastAPI()
    catalog = DeploymentCatalog()
    app.state.deployments = catalog
    app.include_router(router, prefix="/api")

    response = TestClient(app).get(
        f"/api/deployments/{catalog.item.id}/service-logs?service=Invalid_Name"
    )

    assert response.status_code == 422


def test_service_log_stream_returns_sse_contract_and_closes_subscription() -> None:
    app = FastAPI()
    catalog = DeploymentCatalog()
    app.state.deployments = catalog
    app.include_router(router, prefix="/api")

    response = TestClient(app).get(
        f"/api/deployments/{catalog.item.id}/service-logs/stream?service=api",
        headers={"Accept": "text/event-stream"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    assert "event: ready" in response.text
    assert '"serviceName":"api"' in response.text
    assert ": keepalive" in response.text
    assert "event: log" in response.text
    assert '"stream":"STDERR"' in response.text
    assert "event: end" in response.text
    assert catalog.subscription.closed is True


def test_service_log_stream_rejects_noncanonical_service_name() -> None:
    app = FastAPI()
    catalog = DeploymentCatalog()
    app.state.deployments = catalog
    app.include_router(router, prefix="/api")

    response = TestClient(app).get(
        f"/api/deployments/{catalog.item.id}/service-logs/stream?service=Invalid_Name"
    )

    assert response.status_code == 422


def test_deployment_event_stream_uses_largest_resume_cursor_and_public_sse_contract() -> None:
    app = FastAPI()
    catalog = DeploymentCatalog()
    app.state.deployments = catalog
    app.include_router(router, prefix="/api")

    response = TestClient(app).get(
        f"/api/deployments/{catalog.item.id}/events/stream?after=4",
        headers={"Accept": "text/event-stream", "Last-Event-ID": "9"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    assert '"after":9' in response.text
    assert ": keepalive" in response.text
    assert "id: 10" in response.text
    assert "event: deployment-event" in response.text
    assert '"code":"IMAGES_BUILDING"' in response.text
    assert "event: end" in response.text
    assert catalog.event_subscription.closed is True


def test_deployment_event_stream_rejects_invalid_last_event_id() -> None:
    app = FastAPI()
    install_error_handlers(app)
    catalog = DeploymentCatalog()
    app.state.deployments = catalog
    app.include_router(router, prefix="/api")

    response = TestClient(app).get(
        f"/api/deployments/{catalog.item.id}/events/stream",
        headers={"Last-Event-ID": "not-a-number"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_EVENT_CURSOR"
