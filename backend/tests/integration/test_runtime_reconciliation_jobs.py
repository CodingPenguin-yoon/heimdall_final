import os
import time
from datetime import timedelta
from uuid import uuid4

import pytest
from conftest import FakeGit
from test_project_schemas import valid_settings

from heimdall.database import Database
from heimdall.deployments.models import DeploymentClaimLostError
from heimdall.deployments.repository import PostgresDeploymentRepository
from heimdall.deployments.schemas import DeploymentCreate
from heimdall.deployments.service import DeploymentService
from heimdall.projects.repository import PostgresProjectRepository
from heimdall.projects.schemas import ProjectCreate, ProjectSettingsUpdate
from heimdall.projects.service import ProjectService
from heimdall.runtime.reconciliation import (
    ReconciliationClaimLostError,
    ReconciliationRequester,
)
from heimdall.runtime.reconciliation_repository import PostgresRuntimeReconciliationRepository

CONTROL_URL = os.environ.get("HEIMDALL_TEST_CONTROL_DB_URL")

pytestmark = pytest.mark.skipif(
    not CONTROL_URL,
    reason="Control PostgreSQL integration URL is not configured",
)


def test_retention_discovers_uncertain_failure_and_fences_expired_claim() -> None:
    assert CONTROL_URL is not None
    control = Database(CONTROL_URL)
    control.open()
    try:
        projects = ProjectService(PostgresProjectRepository(control), FakeGit())
        deployments = PostgresDeploymentRepository(control)
        service = DeploymentService(deployments, projects)
        run_id = uuid4().hex
        project = projects.create(
            ProjectCreate(
                name=f"Reconcile-{run_id}",
                repositoryUrl=f"https://github.com/example/reconcile-{run_id}",
            )
        )
        projects.update_settings(
            project.id,
            ProjectSettingsUpdate.model_validate(valid_settings()),
        )
        service.request(
            project.id,
            DeploymentCreate.model_validate({"source": {"type": "MAIN_HEAD"}}),
        )
        deployment_claim = deployments.claim_next("deployment-worker", timedelta(seconds=1))
        assert deployment_claim is not None
        failed = deployments.fail(
            deployment_claim,
            "RECOVERY",
            "RECOVERY_STATE_UNCERTAIN",
        )

        reconciliations = PostgresRuntimeReconciliationRepository(control)
        assert failed.terminal_at is not None
        assert service.list_uncertain_before(failed.terminal_at - timedelta(seconds=1)) == []
        assert reconciliations.claim_next("reconciliation-worker", timedelta(seconds=1)) is None
        time.sleep(0.01)
        candidates = service.list_uncertain_before(failed.terminal_at + timedelta(seconds=1))
        assert len(candidates) == 1
        reconciliations.schedule_automatic(
            [(candidates[0].id, candidates[0].terminal_at + timedelta(milliseconds=1))]
        )
        first = reconciliations.claim_next("reconciliation-worker-one", timedelta(milliseconds=100))
        assert first is not None
        assert first.reconciliation.requested_by is ReconciliationRequester.SYSTEM
        assert reconciliations.claim_next("reconciliation-worker-two", timedelta(seconds=1)) is None

        time.sleep(0.12)
        recovered = reconciliations.claim_next(
            "reconciliation-worker-two",
            timedelta(seconds=1),
        )
        assert recovered is not None
        assert recovered.token != first.token
        assert recovered.reconciliation.attempts == 2

        with pytest.raises(ReconciliationClaimLostError):
            reconciliations.block(first, "STALE_RECONCILIATION")
        with pytest.raises(DeploymentClaimLostError):
            deployments.fail(
                deployment_claim,
                "RECOVERY",
                "STALE_DEPLOYMENT_CLAIM",
            )

        blocked = reconciliations.block(recovered, "RECOVERY_STATE_UNCERTAIN")
        assert blocked.state.value == "BLOCKED"
    finally:
        control.close()
