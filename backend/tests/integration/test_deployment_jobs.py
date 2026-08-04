import os
import time
from datetime import timedelta
from uuid import uuid4

import pytest
from conftest import FakeGit
from test_project_schemas import valid_settings

from heimdall.database import Database
from heimdall.deployments.models import DeploymentClaimLostError, DeploymentStatus
from heimdall.deployments.repository import PostgresDeploymentRepository
from heimdall.deployments.schemas import DeploymentCreate
from heimdall.deployments.service import DeploymentService
from heimdall.projects.repository import PostgresProjectRepository
from heimdall.projects.schemas import ProjectCreate, ProjectSettingsUpdate
from heimdall.projects.service import ProjectService

CONTROL_URL = os.environ.get("HEIMDALL_TEST_CONTROL_DB_URL")

pytestmark = pytest.mark.skipif(
    not CONTROL_URL,
    reason="Control PostgreSQL integration URL is not configured",
)


def test_expired_claim_is_recovered_and_old_worker_is_fenced() -> None:
    assert CONTROL_URL is not None
    control = Database(CONTROL_URL)
    control.open()
    try:
        projects = ProjectService(PostgresProjectRepository(control), FakeGit())
        repository = PostgresDeploymentRepository(control)
        deployments = DeploymentService(repository, projects)
        run_id = uuid4().hex
        project = projects.create(
            ProjectCreate(
                name=f"Worker-{run_id}",
                repositoryUrl=f"https://github.com/example/worker-{run_id}",
            )
        )
        project = projects.update_settings(
            project.id,
            ProjectSettingsUpdate.model_validate(valid_settings()),
        )
        deployment = deployments.request(
            project.id,
            DeploymentCreate.model_validate({"source": {"type": "MAIN_HEAD"}}),
        )

        first = repository.claim_next("worker-one", timedelta(milliseconds=100))
        assert first is not None
        assert repository.claim_next("worker-two", timedelta(seconds=1)) is None
        time.sleep(0.12)

        recovered = repository.claim_next("worker-two", timedelta(seconds=1))
        assert recovered is not None
        assert recovered.deployment.id == deployment.id
        assert recovered.token != first.token

        with pytest.raises(DeploymentClaimLostError):
            repository.advance(
                first,
                DeploymentStatus.BUILDING,
                event_code="STALE_WORKER",
                event_message="This event must not be written",
            )

        repository.advance(
            recovered,
            DeploymentStatus.BUILDING,
            event_code="IMAGES_BUILDING",
            event_message="Building service images",
        )
        result = repository.succeed(recovered)

        assert result.status is DeploymentStatus.SUCCEEDED
        events = repository.list_events(deployment.id)
        assert [event.code for event in events] == [
            "JOB_CLAIMED",
            "JOB_CLAIMED",
            "IMAGES_BUILDING",
            "DEPLOYMENT_SUCCEEDED",
        ]
    finally:
        control.close()
