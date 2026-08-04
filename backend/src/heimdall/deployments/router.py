from uuid import UUID

from fastapi import APIRouter, Request, status

from heimdall.deployments.schemas import (
    DeploymentCreate,
    DeploymentEventList,
    DeploymentEventRead,
    DeploymentList,
    DeploymentRead,
)
from heimdall.deployments.service import DeploymentService

router = APIRouter()


def service(request: Request) -> DeploymentService:
    return request.app.state.deployments


@router.post(
    "/projects/{project_id}/deployments",
    response_model=DeploymentRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_deployment(
    project_id: UUID, payload: DeploymentCreate, request: Request
) -> DeploymentRead:
    return DeploymentRead.from_deployment(service(request).request(project_id, payload))


@router.get("/projects/{project_id}/deployments", response_model=DeploymentList)
def list_deployments(project_id: UUID, request: Request) -> DeploymentList:
    return DeploymentList(
        items=[
            DeploymentRead.from_deployment(item)
            for item in service(request).list_for_project(project_id)
        ]
    )


@router.get("/deployments/{deployment_id}", response_model=DeploymentRead)
def get_deployment(deployment_id: UUID, request: Request) -> DeploymentRead:
    return DeploymentRead.from_deployment(service(request).get(deployment_id))


@router.get("/deployments/{deployment_id}/events", response_model=DeploymentEventList)
def list_deployment_events(deployment_id: UUID, request: Request) -> DeploymentEventList:
    return DeploymentEventList(
        items=[
            DeploymentEventRead.from_event(item) for item in service(request).events(deployment_id)
        ]
    )
