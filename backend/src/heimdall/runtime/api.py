from uuid import UUID

from fastapi import APIRouter, Request, status

from heimdall.runtime.reconciliation_service import RuntimeReconciliationService
from heimdall.runtime.schemas import (
    ProjectRuntimeRead,
    RuntimeReconciliationRead,
    RuntimeReconciliationRequest,
)
from heimdall.runtime.status import RuntimeStatusService

router = APIRouter()


def service(request: Request) -> RuntimeStatusService:
    return request.app.state.runtime_status


def reconciliation_service(request: Request) -> RuntimeReconciliationService:
    return request.app.state.runtime_reconciliations


@router.get("/projects/{project_id}/runtime", response_model=ProjectRuntimeRead)
def get_project_runtime(project_id: UUID, request: Request) -> ProjectRuntimeRead:
    return ProjectRuntimeRead.from_runtime(service(request).get(project_id))


@router.get(
    "/deployments/{deployment_id}/runtime-reconciliation",
    response_model=RuntimeReconciliationRead,
)
def get_runtime_reconciliation(deployment_id: UUID, request: Request) -> RuntimeReconciliationRead:
    return RuntimeReconciliationRead.from_view(reconciliation_service(request).get(deployment_id))


@router.post(
    "/deployments/{deployment_id}/runtime-reconciliation",
    response_model=RuntimeReconciliationRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_runtime_reconciliation(
    deployment_id: UUID,
    payload: RuntimeReconciliationRequest,
    request: Request,
) -> RuntimeReconciliationRead:
    return RuntimeReconciliationRead.from_view(
        reconciliation_service(request).request(
            deployment_id,
            payload.action,
            payload.confirmation,
        )
    )
