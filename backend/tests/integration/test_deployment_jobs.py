import json
import os
import time
from datetime import timedelta
from uuid import uuid4

import pytest
from conftest import FakeGit
from test_project_schemas import valid_settings

from heimdall.database import Database
from heimdall.deployments.models import DeploymentClaimLostError, DeploymentStatus
from heimdall.deployments.repository import DEPLOYMENT_EVENT_CHANNEL, PostgresDeploymentRepository
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
        deadline = time.monotonic() + 1
        recovered = None
        while recovered is None and time.monotonic() < deadline:
            recovered = repository.claim_next("worker-two", timedelta(seconds=1))
            if recovered is None:
                time.sleep(0.01)
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


def test_deployment_event_insert_notifies_listener_and_supports_cursor_replay() -> None:
    assert CONTROL_URL is not None
    control = Database(CONTROL_URL)
    control.open()
    try:
        projects = ProjectService(PostgresProjectRepository(control), FakeGit())
        repository = PostgresDeploymentRepository(control)
        service = DeploymentService(repository, projects)
        run_id = uuid4().hex
        project = projects.create(
            ProjectCreate(
                name=f"Events-{run_id}",
                repositoryUrl=f"https://github.com/example/events-{run_id}",
            )
        )
        project = projects.update_settings(
            project.id,
            ProjectSettingsUpdate.model_validate(valid_settings()),
        )
        deployment = service.request(
            project.id,
            DeploymentCreate.model_validate({"source": {"type": "MAIN_HEAD"}}),
        )

        with control.connection() as listener:
            listener.execute(f"LISTEN {DEPLOYMENT_EVENT_CHANNEL}")
            listener.commit()
            claim = repository.claim_next("event-listener", timedelta(seconds=1))
            assert claim is not None

            notifications = list(listener.notifies(timeout=1, stop_after=1))

        assert len(notifications) == 1
        payload = json.loads(notifications[0].payload)
        assert payload["deploymentId"] == str(deployment.id)
        assert set(payload) == {"deploymentId", "eventId"}
        replay = repository.list_events_after(deployment.id, 0)
        assert [item.id for item in replay] == [payload["eventId"]]
    finally:
        control.close()
