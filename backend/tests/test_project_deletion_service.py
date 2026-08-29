from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from conftest import FakeGit, MemoryProjects
from test_project_schemas import valid_settings

from heimdall.common.errors import AppError
from heimdall.projects.models import (
    ProjectDeletionConflictError,
    ProjectDeletionJob,
    ProjectDeletionNotFoundError,
    ProjectDeletionPhase,
    ProjectDeletionState,
    ProjectDeletionValidationError,
    ProjectStatus,
)
from heimdall.projects.schemas import ProjectCreate, ProjectDeletionRequest, ProjectSettingsUpdate
from heimdall.projects.service import ProjectService


class DeletableProjects(MemoryProjects):
    def __init__(self) -> None:
        super().__init__()
        self.jobs: dict[UUID, ProjectDeletionJob] = {}
        self.database_projects: set[UUID] = set()

    def request_deletion(
        self,
        project_id: UUID,
        *,
        confirmation: str,
        delete_managed_database: bool,
        managed_database_confirmation: str | None,
    ) -> ProjectDeletionJob:
        project = self.items[project_id]
        if confirmation != str(project_id):
            raise ProjectDeletionValidationError("PROJECT_DELETE_CONFIRMATION_MISMATCH")
        has_database = project_id in self.database_projects
        expected = f"DELETE {project_id} APPLICATION DATA"
        if has_database and (
            not delete_managed_database or managed_database_confirmation != expected
        ):
            raise ProjectDeletionValidationError("PROJECT_DATABASE_DELETE_CONFIRMATION_REQUIRED")
        current = self.jobs.get(project_id)
        if current is not None:
            if current.state is ProjectDeletionState.FAILED:
                raise ProjectDeletionConflictError("PROJECT_DELETION_FAILED")
            return current
        now = datetime.now(UTC)
        self.items[project_id] = replace(project, status=ProjectStatus.DELETING, updated_at=now)
        job = ProjectDeletionJob(
            project_id=project_id,
            state=ProjectDeletionState.PENDING,
            phase=ProjectDeletionPhase.REQUESTED,
            attempts=0,
            available_at=now,
            lease_owner=None,
            lease_expires_at=None,
            claim_token=None,
            last_error_code=None,
            last_error_retryable=None,
            delete_managed_database=has_database,
            created_at=now,
            updated_at=now,
        )
        self.jobs[project_id] = job
        return job

    def get_deletion(self, project_id: UUID) -> ProjectDeletionJob:
        self.items[project_id]
        try:
            return self.jobs[project_id]
        except KeyError as error:
            raise ProjectDeletionNotFoundError from error

    def retry_deletion(
        self,
        project_id: UUID,
        *,
        confirmation: str,
        delete_managed_database: bool,
        managed_database_confirmation: str | None,
    ) -> ProjectDeletionJob:
        if confirmation != str(project_id):
            raise ProjectDeletionValidationError("PROJECT_DELETE_CONFIRMATION_MISMATCH")
        expected = f"DELETE {project_id} APPLICATION DATA"
        if project_id in self.database_projects and (
            not delete_managed_database or managed_database_confirmation != expected
        ):
            raise ProjectDeletionValidationError("PROJECT_DATABASE_DELETE_CONFIRMATION_REQUIRED")
        try:
            current = self.jobs[project_id]
        except KeyError as error:
            raise ProjectDeletionNotFoundError from error
        if current.state is not ProjectDeletionState.FAILED:
            raise ProjectDeletionConflictError("PROJECT_DELETION_NOT_FAILED")
        retried = replace(
            current,
            state=ProjectDeletionState.PENDING,
            available_at=datetime.now(UTC),
            lease_owner=None,
            lease_expires_at=None,
            claim_token=None,
            last_error_code=None,
            last_error_retryable=None,
            updated_at=datetime.now(UTC),
        )
        self.jobs[project_id] = retried
        return retried


def deletion_request(project_id: UUID, *, managed: bool = False) -> ProjectDeletionRequest:
    payload: dict[str, object] = {"confirmation": str(project_id)}
    if managed:
        payload.update(
            {
                "deleteManagedDatabase": True,
                "managedDatabaseConfirmation": f"DELETE {project_id} APPLICATION DATA",
            }
        )
    return ProjectDeletionRequest.model_validate(payload)


def test_deletion_requires_the_exact_full_project_uuid() -> None:
    repository = DeletableProjects()
    service = ProjectService(repository, FakeGit())
    project = service.create(
        ProjectCreate(name="Console", repositoryUrl="https://github.com/example/console")
    )
    request = ProjectDeletionRequest.model_validate({"confirmation": str(uuid4())})

    with pytest.raises(AppError) as raised:
        service.delete(project.id, request)

    assert raised.value.status == 422
    assert raised.value.code == "PROJECT_DELETE_CONFIRMATION_MISMATCH"
    assert repository.items[project.id].status is ProjectStatus.DRAFT


def test_managed_database_requires_separate_application_data_confirmation() -> None:
    repository = DeletableProjects()
    service = ProjectService(repository, FakeGit())
    project = service.create(
        ProjectCreate(name="Console", repositoryUrl="https://github.com/example/console")
    )
    repository.database_projects.add(project.id)

    with pytest.raises(AppError) as raised:
        service.delete(project.id, deletion_request(project.id))

    assert raised.value.status == 422
    assert raised.value.code == "PROJECT_DATABASE_DELETE_CONFIRMATION_REQUIRED"
    assert repository.items[project.id].status is ProjectStatus.DRAFT


def test_delete_marks_project_deleting_and_reuses_pending_job() -> None:
    repository = DeletableProjects()
    service = ProjectService(repository, FakeGit())
    project = service.create(
        ProjectCreate(name="Console", repositoryUrl="https://github.com/example/console")
    )

    first = service.delete(project.id, deletion_request(project.id))
    second = service.delete(project.id, deletion_request(project.id))

    assert first == second
    assert first.state is ProjectDeletionState.PENDING
    assert first.phase is ProjectDeletionPhase.REQUESTED
    assert repository.items[project.id].status is ProjectStatus.DELETING


def test_failed_deletion_rejects_delete_and_explicit_retry_requeues_it() -> None:
    repository = DeletableProjects()
    service = ProjectService(repository, FakeGit())
    project = service.create(
        ProjectCreate(name="Console", repositoryUrl="https://github.com/example/console")
    )
    job = service.delete(project.id, deletion_request(project.id))
    repository.jobs[project.id] = replace(
        job,
        state=ProjectDeletionState.FAILED,
        last_error_code="PROJECT_RESOURCES_UNCERTAIN",
        last_error_retryable=True,
    )

    with pytest.raises(AppError) as raised:
        service.delete(project.id, deletion_request(project.id))
    retried = service.retry_deletion(project.id, deletion_request(project.id))

    assert raised.value.status == 409
    assert raised.value.code == "PROJECT_DELETION_FAILED"
    assert retried.state is ProjectDeletionState.PENDING
    assert retried.last_error_code is None


def test_retry_without_a_deletion_job_is_not_found() -> None:
    repository = DeletableProjects()
    service = ProjectService(repository, FakeGit())
    project = service.create(
        ProjectCreate(name="Console", repositoryUrl="https://github.com/example/console")
    )

    with pytest.raises(AppError) as raised:
        service.retry_deletion(project.id, deletion_request(project.id))

    assert raised.value.status == 404
    assert raised.value.code == "PROJECT_DELETION_NOT_FOUND"


def test_deleting_project_rejects_settings_and_ready_operations() -> None:
    repository = DeletableProjects()
    service = ProjectService(repository, FakeGit())
    project = service.create(
        ProjectCreate(name="Console", repositoryUrl="https://github.com/example/console")
    )
    service.delete(project.id, deletion_request(project.id))

    with pytest.raises(AppError) as ready_error:
        service.ready(project.id)
    with pytest.raises(AppError) as settings_error:
        service.update_settings(
            project.id,
            ProjectSettingsUpdate.model_validate(valid_settings()),
        )

    assert ready_error.value.status == 409
    assert ready_error.value.code == "PROJECT_DELETING"
    assert settings_error.value.status == 409
    assert settings_error.value.code == "PROJECT_DELETING"
