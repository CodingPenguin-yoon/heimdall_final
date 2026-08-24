from uuid import UUID

from fastapi import APIRouter, Request, status

from heimdall.public_routes.schemas import PublicRouteRead, PublicRouteUpdate
from heimdall.public_routes.service import PublicRouteService

router = APIRouter()


def service(request: Request) -> PublicRouteService:
    return request.app.state.public_routes


@router.get("/{project_id}/public-route", response_model=PublicRouteRead)
def get_public_route(project_id: UUID, request: Request) -> PublicRouteRead:
    return PublicRouteRead.from_route(service(request).get(project_id))


@router.put(
    "/{project_id}/public-route",
    response_model=PublicRouteRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def set_public_route(
    project_id: UUID,
    payload: PublicRouteUpdate,
    request: Request,
) -> PublicRouteRead:
    return PublicRouteRead.from_route(service(request).set(project_id, payload))


@router.delete(
    "/{project_id}/public-route",
    response_model=PublicRouteRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def disable_public_route(project_id: UUID, request: Request) -> PublicRouteRead:
    return PublicRouteRead.from_route(service(request).disable(project_id))
