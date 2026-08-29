from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from heimdall.projects.models import (
    ProjectDeletionJob,
    ProjectDeletionPhase,
    ProjectDeletionState,
)
from heimdall.projects.router import router


class DeletionService:
    def __init__(self) -> None:
        now = datetime(2026, 8, 29, tzinfo=UTC)
        self.project_id = uuid4()
        self.job = ProjectDeletionJob(
            project_id=self.project_id,
            state=ProjectDeletionState.PENDING,
            phase=ProjectDeletionPhase.REQUESTED,
            attempts=0,
            available_at=now,
            lease_owner=None,
            lease_expires_at=None,
            claim_token=None,
            last_error_code=None,
            last_error_retryable=None,
            delete_managed_database=True,
            created_at=now,
            updated_at=now,
        )
        self.calls = []

    def delete(self, project_id, payload):
        self.calls.append(("delete", project_id, payload))
        return self.job

    def deletion(self, project_id):
        self.calls.append(("get", project_id))
        return self.job

    def retry_deletion(self, project_id, payload):
        self.calls.append(("retry", project_id, payload))
        return self.job


def test_deletion_routes_return_public_job_without_claim_credentials() -> None:
    app = FastAPI()
    service = DeletionService()
    app.state.projects = service
    app.include_router(router, prefix="/api/projects")
    client = TestClient(app)
    body = {
        "confirmation": str(service.project_id),
        "deleteManagedDatabase": True,
        "managedDatabaseConfirmation": (f"DELETE {service.project_id} APPLICATION DATA"),
    }

    deleted = client.request("DELETE", f"/api/projects/{service.project_id}", json=body)
    fetched = client.get(f"/api/projects/{service.project_id}/deletion")
    retried = client.post(f"/api/projects/{service.project_id}/deletion/retry", json=body)

    assert deleted.status_code == 202
    assert fetched.status_code == 200
    assert retried.status_code == 202
    assert deleted.json() == fetched.json() == retried.json()
    assert deleted.json()["projectId"] == str(service.project_id)
    assert deleted.json()["state"] == "PENDING"
    assert deleted.json()["phase"] == "REQUESTED"
    assert "claimToken" not in deleted.json()
    assert "leaseOwner" not in deleted.json()
    assert "leaseExpiresAt" not in deleted.json()
