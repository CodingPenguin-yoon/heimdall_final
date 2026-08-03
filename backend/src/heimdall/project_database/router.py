from uuid import UUID

from fastapi import APIRouter, Request, status

from heimdall.project_database.schemas import ProjectDatabaseRead
from heimdall.project_database.service import ProjectDatabaseService

router = APIRouter()


def service(request: Request) -> ProjectDatabaseService:
    return request.app.state.project_databases


@router.get("/projects/{project_id}/database", response_model=ProjectDatabaseRead)
def get_project_database(project_id: UUID, request: Request) -> ProjectDatabaseRead:
    return service(request).status(project_id)


@router.post(
    "/projects/{project_id}/database",
    response_model=ProjectDatabaseRead,
    status_code=status.HTTP_201_CREATED,
)
def provision_project_database(project_id: UUID, request: Request) -> ProjectDatabaseRead:
    return service(request).provision(project_id)
