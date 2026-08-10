from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_runtime_models import runtime_deployment

from heimdall.deployments.router import router


class DeploymentCatalog:
    def __init__(self) -> None:
        self.item = runtime_deployment()

    def list_recent(self):
        return [self.item]


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
