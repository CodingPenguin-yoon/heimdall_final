from uuid import UUID

from fastapi import APIRouter, Request, status

from heimdall.projects.schemas import (
    CommitList,
    CommitRead,
    ProjectCreate,
    ProjectDeletionRead,
    ProjectDeletionRequest,
    ProjectList,
    ProjectRead,
    ProjectSettingsUpdate,
)
from heimdall.projects.service import ProjectService

router = APIRouter()


def service(request: Request) -> ProjectService:
    return request.app.state.projects


@router.post("", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
def create_project(payload: ProjectCreate, request: Request) -> ProjectRead:
    return ProjectRead.from_project(service(request).create(payload))


@router.get("", response_model=ProjectList)
def list_projects(request: Request) -> ProjectList:
    return ProjectList(items=[ProjectRead.from_project(item) for item in service(request).list()])


@router.get("/{project_id}", response_model=ProjectRead)
def get_project(project_id: UUID, request: Request) -> ProjectRead:
    return ProjectRead.from_project(service(request).get(project_id))


@router.delete(
    "/{project_id}",
    response_model=ProjectDeletionRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def delete_project(
    project_id: UUID, payload: ProjectDeletionRequest, request: Request
) -> ProjectDeletionRead:
    return ProjectDeletionRead.from_job(service(request).delete(project_id, payload))


@router.get("/{project_id}/deletion", response_model=ProjectDeletionRead)
def get_project_deletion(project_id: UUID, request: Request) -> ProjectDeletionRead:
    return ProjectDeletionRead.from_job(service(request).deletion(project_id))


@router.post(
    "/{project_id}/deletion/retry",
    response_model=ProjectDeletionRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_project_deletion(
    project_id: UUID, payload: ProjectDeletionRequest, request: Request
) -> ProjectDeletionRead:
    return ProjectDeletionRead.from_job(service(request).retry_deletion(project_id, payload))


@router.put("/{project_id}/settings", response_model=ProjectRead)
def update_project_settings(
    project_id: UUID, payload: ProjectSettingsUpdate, request: Request
) -> ProjectRead:
    return ProjectRead.from_project(service(request).update_settings(project_id, payload))


@router.get("/{project_id}/commits", response_model=CommitList)
def list_project_commits(project_id: UUID, request: Request) -> CommitList:
    return CommitList(
        items=[
            CommitRead(
                sha=commit.sha,
                author_name=commit.author_name,
                committed_at=commit.committed_at,
                subject=commit.subject,
            )
            for commit in service(request).commits(project_id)
        ]
    )
