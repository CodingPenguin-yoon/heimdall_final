from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_runtime_models import runtime_deployment

from heimdall.deployments.router import router
from heimdall.runtime.logs import ServiceLogLine, ServiceLogSnapshot, ServiceLogStream


class DeploymentCatalog:
    def __init__(self) -> None:
        self.item = runtime_deployment()

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


def test_service_logs_reject_noncanonical_service_name_before_the_service_layer() -> None:
    app = FastAPI()
    catalog = DeploymentCatalog()
    app.state.deployments = catalog
    app.include_router(router, prefix="/api")

    response = TestClient(app).get(
        f"/api/deployments/{catalog.item.id}/service-logs?service=Invalid_Name"
    )

    assert response.status_code == 422
