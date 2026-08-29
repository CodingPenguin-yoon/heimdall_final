from __future__ import annotations

import os
import time
from datetime import timedelta
from uuid import uuid4

import pytest
from conftest import FakeGit
from test_project_schemas import valid_settings

from heimdall.database import Database
from heimdall.deployments.models import DeploymentSource
from heimdall.deployments.repository import PostgresDeploymentRepository
from heimdall.project_database.repository import PostgresProjectDatabaseRepository
from heimdall.projects.models import (
    ProjectDeletionClaimLostError,
    ProjectDeletionPhase,
    ProjectDeletionState,
    ProjectStatus,
)
from heimdall.projects.repository import PostgresProjectRepository
from heimdall.projects.schemas import ProjectCreate, ProjectSettingsUpdate
from heimdall.projects.service import ProjectService
from heimdall.public_routes.repository import PostgresPublicRouteRepository
from heimdall.runtime.reconciliation import (
    ReconciliationAction,
    ReconciliationRequester,
)
from heimdall.runtime.reconciliation_repository import (
    PostgresRuntimeReconciliationRepository,
)

CONTROL_URL = os.environ.get("HEIMDALL_TEST_CONTROL_DB_URL")

pytestmark = pytest.mark.skipif(
    not CONTROL_URL,
    reason="Control PostgreSQL integration URL is not configured",
)


def create_project(repository: PostgresProjectRepository):
    service = ProjectService(repository, FakeGit())
    suffix = uuid4().hex
    return service.create(
        ProjectCreate(
            name=f"Delete-{suffix}",
            repositoryUrl=f"https://github.com/example/delete-{suffix}",
        )
    )


def cleanup_project(database: Database, project_id) -> None:
    with database.connection() as connection:
        connection.execute(
            "DELETE FROM project_database_resources WHERE project_id = %s", (project_id,)
        )
        connection.execute("DELETE FROM project_deletion_jobs WHERE project_id = %s", (project_id,))
        connection.execute("DELETE FROM project_public_routes WHERE project_id = %s", (project_id,))
        connection.execute("DELETE FROM deployments WHERE project_id = %s", (project_id,))
        connection.execute(
            "DELETE FROM project_environment_secrets WHERE project_id = %s", (project_id,)
        )
        connection.execute("DELETE FROM projects WHERE id = %s", (project_id,))


def test_delete_intent_is_atomic_idempotent_and_requires_managed_database_confirmation() -> None:
    assert CONTROL_URL is not None
    database = Database(CONTROL_URL)
    database.open()
    project = None
    try:
        repository = PostgresProjectRepository(database)
        project = create_project(repository)
        now = project.created_at
        with database.connection() as connection:
            connection.execute(
                """
                INSERT INTO project_database_resources (
                    id, project_id, desired_state, status, phase,
                    database_name, role_name, schema_name, created_at, updated_at
                ) VALUES (%s, %s, 'ACTIVE', 'ACTIVE', 'ACTIVE', %s, %s, 'app', %s, %s)
                """,
                (
                    uuid4(),
                    project.id,
                    f"hd_db_{uuid4().hex}",
                    f"hd_role_{uuid4().hex}",
                    now,
                    now,
                ),
            )

        with pytest.raises(Exception, match="PROJECT_DATABASE_DELETE_CONFIRMATION_REQUIRED"):
            repository.request_deletion(
                project.id,
                confirmation=str(project.id),
                delete_managed_database=False,
                managed_database_confirmation=None,
            )

        confirmation = f"DELETE {project.id} APPLICATION DATA"
        first = repository.request_deletion(
            project.id,
            confirmation=str(project.id),
            delete_managed_database=True,
            managed_database_confirmation=confirmation,
        )
        second = repository.request_deletion(
            project.id,
            confirmation=str(project.id),
            delete_managed_database=True,
            managed_database_confirmation=confirmation,
        )

        assert first == second
        assert first.delete_managed_database is True
        assert repository.get(project.id).status is ProjectStatus.DELETING
    finally:
        if project is not None:
            cleanup_project(database, project.id)
        database.close()


def test_expired_deletion_claim_is_recovered_and_stale_worker_cannot_advance() -> None:
    assert CONTROL_URL is not None
    database = Database(CONTROL_URL)
    database.open()
    project = None
    try:
        repository = PostgresProjectRepository(database)
        project = create_project(repository)
        repository.request_deletion(
            project.id,
            confirmation=str(project.id),
            delete_managed_database=False,
            managed_database_confirmation=None,
        )

        first = repository.claim_next_deletion("worker-one", timedelta(milliseconds=50))
        assert first is not None
        assert repository.claim_next_deletion("worker-two", timedelta(seconds=1)) is None
        recovered = None
        deadline = time.monotonic() + 1
        while recovered is None and time.monotonic() < deadline:
            recovered = repository.claim_next_deletion("worker-two", timedelta(seconds=1))
            if recovered is None:
                time.sleep(0.01)
        assert recovered is not None
        assert recovered.token != first.token

        with pytest.raises(ProjectDeletionClaimLostError):
            repository.advance_deletion(
                first,
                ProjectDeletionPhase.REQUESTED,
                ProjectDeletionPhase.WAITING_FOR_OPERATIONS,
            )

        advanced = repository.advance_deletion(
            recovered,
            ProjectDeletionPhase.REQUESTED,
            ProjectDeletionPhase.WAITING_FOR_OPERATIONS,
        )
        assert advanced.phase is ProjectDeletionPhase.WAITING_FOR_OPERATIONS
    finally:
        if project is not None:
            cleanup_project(database, project.id)
        database.close()


def test_failed_job_is_preserved_and_only_explicit_retry_requeues_it() -> None:
    assert CONTROL_URL is not None
    database = Database(CONTROL_URL)
    database.open()
    project = None
    try:
        repository = PostgresProjectRepository(database)
        project = create_project(repository)
        repository.request_deletion(
            project.id,
            confirmation=str(project.id),
            delete_managed_database=False,
            managed_database_confirmation=None,
        )
        claim = repository.claim_next_deletion("worker", timedelta(seconds=1))
        assert claim is not None
        advanced = repository.advance_deletion(
            claim,
            ProjectDeletionPhase.REQUESTED,
            ProjectDeletionPhase.WAITING_FOR_OPERATIONS,
        )
        claim = type(claim)(
            job=advanced,
            token=claim.token,
            worker_id=claim.worker_id,
            lease_expires_at=claim.lease_expires_at,
        )
        failed = repository.fail_deletion(claim, "PROJECT_RESOURCES_UNCERTAIN", retryable=True)

        assert failed.state is ProjectDeletionState.FAILED
        assert repository.get(project.id).status is ProjectStatus.DELETING
        retried = repository.retry_deletion(
            project.id,
            confirmation=str(project.id),
            delete_managed_database=False,
            managed_database_confirmation=None,
        )
        assert retried.state is ProjectDeletionState.PENDING
        assert retried.phase is ProjectDeletionPhase.WAITING_FOR_OPERATIONS
        assert retried.last_error_code is None
    finally:
        if project is not None:
            cleanup_project(database, project.id)
        database.close()


def test_live_deletion_claim_can_renew_its_lease() -> None:
    assert CONTROL_URL is not None
    database = Database(CONTROL_URL)
    database.open()
    project = None
    try:
        repository = PostgresProjectRepository(database)
        project = create_project(repository)
        repository.request_deletion(
            project.id,
            confirmation=str(project.id),
            delete_managed_database=False,
            managed_database_confirmation=None,
        )
        claim = repository.claim_next_deletion("worker-one", timedelta(milliseconds=50))
        assert claim is not None

        renewed_until = repository.renew_deletion(claim, timedelta(seconds=1))
        time.sleep(0.06)

        assert renewed_until > claim.lease_expires_at
        assert repository.claim_next_deletion("worker-two", timedelta(seconds=1)) is None
    finally:
        if project is not None:
            cleanup_project(database, project.id)
        database.close()


def test_deleting_project_is_fenced_inside_each_db_intent_transaction() -> None:
    assert CONTROL_URL is not None
    database = Database(CONTROL_URL)
    database.open()
    project = None
    try:
        projects = PostgresProjectRepository(database)
        service = ProjectService(projects, FakeGit())
        project = create_project(projects)
        ready = service.update_settings(
            project.id,
            ProjectSettingsUpdate.model_validate(valid_settings()),
        )
        deployments = PostgresDeploymentRepository(database)
        existing_deployment = deployments.create(
            project_id=project.id,
            source_type=DeploymentSource.MAIN_HEAD,
            requested_commit_sha=None,
            resolved_commit_sha="a" * 40,
            config_version=ready.config_version,
            config_snapshot=ready.deployment_config or {},
        )
        with database.connection() as connection:
            connection.execute(
                "UPDATE deployments SET status = 'SUCCEEDED', terminal_at = now() WHERE id = %s",
                (existing_deployment.id,),
            )
            connection.execute(
                "UPDATE deployment_jobs SET state = 'DONE' WHERE deployment_id = %s",
                (existing_deployment.id,),
            )
        projects.request_deletion(
            project.id,
            confirmation=str(project.id),
            delete_managed_database=False,
            managed_database_confirmation=None,
        )

        with pytest.raises(RuntimeError, match="PROJECT_DELETING"):
            projects.update_settings(
                project.id,
                ready.config_version,
                ready.deployment_config or {},
                [],
            )
        with pytest.raises(RuntimeError, match="PROJECT_DELETING"):
            deployments.create(
                project_id=project.id,
                source_type=DeploymentSource.MAIN_HEAD,
                requested_commit_sha=None,
                resolved_commit_sha="b" * 40,
                config_version=ready.config_version,
                config_snapshot=ready.deployment_config or {},
            )
        with pytest.raises(RuntimeError, match="PROJECT_DELETING"):
            PostgresProjectDatabaseRepository(database).ensure_intent(project.id)
        with pytest.raises(RuntimeError, match="PROJECT_DELETING"):
            PostgresPublicRouteRepository(database).set_enabled(
                project.id, "delete-fenced", "delete-fenced.example.test"
            )
        with pytest.raises(RuntimeError, match="PROJECT_DELETING"):
            PostgresRuntimeReconciliationRepository(database).request(
                existing_deployment.id,
                ReconciliationAction.RECONCILE,
                ReconciliationRequester.ADMIN,
            )
        reconciliations = PostgresRuntimeReconciliationRepository(database)
        reconciliations.schedule_automatic([(existing_deployment.id, ready.updated_at)])
        assert reconciliations.get(existing_deployment.id) is None
    finally:
        if project is not None:
            cleanup_project(database, project.id)
        database.close()
