import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from conftest import FakeGit
from test_project_schemas import valid_settings

from heimdall.database import Database
from heimdall.deployments.diagnostics import (
    DiagnosticArtifactDraft,
    DiagnosticArtifactKind,
    DiagnosticCaptureStatus,
    DiagnosticLine,
    DiagnosticStream,
)
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


def test_diagnostic_event_and_payload_are_atomic_bounded_and_expirable() -> None:
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
                name=f"Diagnostics-{run_id}",
                repositoryUrl=f"https://github.com/example/diagnostics-{run_id}",
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
        claim = repository.claim_next("diagnostic-worker", timedelta(minutes=1))
        assert claim is not None
        captured_at = datetime.now(UTC)
        artifact = DiagnosticArtifactDraft(
            kind=DiagnosticArtifactKind.COMMAND_OUTPUT,
            capture_status=DiagnosticCaptureStatus.CAPTURED,
            capture_code=None,
            operation="DOCKER_BUILD",
            service_name=None,
            return_code=17,
            container_status=None,
            container_exit_code=None,
            lines=(DiagnosticLine(DiagnosticStream.STDERR, "build failed"),),
            truncated=False,
            captured_at=captured_at,
        )

        event = repository.record_diagnostics(
            claim,
            failure_stage="BUILD",
            failure_code="IMAGE_BUILD_FAILED",
            artifacts=(artifact,),
            retention=timedelta(days=30),
        )

        metadata = repository.list_diagnostics(deployment.id)
        assert event.code == "DIAGNOSTIC_LOG_CAPTURED"
        assert len(metadata) == 1
        assert metadata[0].event_id == event.id
        assert metadata[0].lines is None
        detail = repository.get_diagnostic(deployment.id, metadata[0].id)
        assert detail.lines == artifact.lines

        with pytest.raises(ValueError):
            repository.record_diagnostics(
                claim,
                failure_stage="BUILD",
                failure_code="IMAGE_BUILD_FAILED",
                artifacts=(
                    replace(
                        artifact,
                        lines=(DiagnosticLine(DiagnosticStream.STDERR, "x" * 300_000),),
                    ),
                ),
                retention=timedelta(days=30),
            )
        assert [item.code for item in repository.list_events(deployment.id)].count(
            "DIAGNOSTIC_LOG_CAPTURED"
        ) == 1

        with control.connection() as connection:
            connection.execute(
                "UPDATE deployment_diagnostic_artifacts SET expires_at = %s WHERE id = %s",
                (datetime.now(UTC) - timedelta(seconds=1), metadata[0].id),
            )
        assert repository.list_diagnostics(deployment.id) == []
        assert repository.purge_expired_diagnostics(limit=1) == 1
        assert any(item.id == event.id for item in repository.list_events(deployment.id))
    finally:
        control.close()
