from uuid import UUID

from fastapi import APIRouter, Request

from heimdall.runtime.schemas import ProjectRuntimeRead
from heimdall.runtime.status import RuntimeStatusService

router = APIRouter()


def service(request: Request) -> RuntimeStatusService:
    return request.app.state.runtime_status


@router.get("/projects/{project_id}/runtime", response_model=ProjectRuntimeRead)
def get_project_runtime(project_id: UUID, request: Request) -> ProjectRuntimeRead:
    return ProjectRuntimeRead.from_runtime(service(request).get(project_id))
