from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from heimdall.common.errors import AppError
from heimdall.deployments.event_sse import deployment_event_sse_events
from heimdall.deployments.log_stream import service_log_sse_events
from heimdall.deployments.schemas import (
    DeploymentCreate,
    DeploymentEventList,
    DeploymentEventRead,
    DeploymentList,
    DeploymentRead,
    ServiceLogRead,
)
from heimdall.deployments.service import DeploymentService

router = APIRouter()
MAX_EVENT_ID = 2**63 - 1


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


@router.get("/deployments", response_model=DeploymentList)
def list_recent_deployments(request: Request) -> DeploymentList:
    return DeploymentList(
        items=[DeploymentRead.from_deployment(item) for item in service(request).list_recent()]
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


@router.get("/deployments/{deployment_id}/events/stream")
def stream_deployment_events(
    deployment_id: UUID,
    request: Request,
    after: Annotated[int, Query(ge=0, le=MAX_EVENT_ID)] = 0,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    header_cursor = 0
    if last_event_id is not None:
        if not last_event_id.isascii() or not last_event_id.isdigit():
            raise AppError(
                400,
                "INVALID_EVENT_CURSOR",
                "Last-Event-ID must be a non-negative bigint",
            )
        header_cursor = int(last_event_id)
        if header_cursor > MAX_EVENT_ID:
            raise AppError(
                400,
                "INVALID_EVENT_CURSOR",
                "Last-Event-ID must be a non-negative bigint",
            )
    subscription = service(request).open_event_stream(deployment_id, max(after, header_cursor))

    return StreamingResponse(
        deployment_event_sse_events(subscription),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/deployments/{deployment_id}/service-logs", response_model=ServiceLogRead)
def get_service_logs(
    deployment_id: UUID,
    request: Request,
    response: Response,
    service_name: Annotated[
        str | None,
        Query(
            alias="service",
            min_length=1,
            max_length=32,
            pattern=r"^[a-z][a-z0-9-]{0,31}$",
        ),
    ] = None,
) -> ServiceLogRead:
    response.headers["Cache-Control"] = "no-store"
    return ServiceLogRead.from_snapshot(service(request).service_logs(deployment_id, service_name))


@router.get("/deployments/{deployment_id}/service-logs/stream")
def stream_service_logs(
    deployment_id: UUID,
    request: Request,
    service_name: Annotated[
        str | None,
        Query(
            alias="service",
            min_length=1,
            max_length=32,
            pattern=r"^[a-z][a-z0-9-]{0,31}$",
        ),
    ] = None,
) -> StreamingResponse:
    subscription = service(request).open_service_log_stream(deployment_id, service_name)

    return StreamingResponse(
        service_log_sse_events(subscription),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
