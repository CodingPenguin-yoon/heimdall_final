from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field

from heimdall.common.api_model import ApiModel
from heimdall.runtime.reconciliation import (
    ReconciliationAction,
    ReconciliationRequester,
    ReconciliationResult,
)
from heimdall.runtime.reconciliation_service import RuntimeReconciliationView
from heimdall.runtime.repository import ProjectRuntime


class ProjectRuntimeRead(ApiModel):
    status: Literal["NOT_ACTIVE", "ACTIVE"]
    preview_port: int | None
    active_deployment_id: UUID | None
    updated_at: datetime | None

    @classmethod
    def from_runtime(cls, runtime: ProjectRuntime | None) -> Self:
        if runtime is None or runtime.active_deployment_id is None:
            return cls(
                status="NOT_ACTIVE",
                preview_port=runtime.preview_port if runtime is not None else None,
                active_deployment_id=None,
                updated_at=runtime.updated_at if runtime is not None else None,
            )
        return cls(
            status="ACTIVE",
            preview_port=runtime.preview_port,
            active_deployment_id=runtime.active_deployment_id,
            updated_at=runtime.updated_at,
        )


class RuntimeReconciliationRequest(ApiModel):
    action: ReconciliationAction
    confirmation: Annotated[str, Field(min_length=36, max_length=36)] | None = None


class RuntimeReconciliationRead(ApiModel):
    deployment_id: UUID
    state: Literal["RETAINED", "PENDING", "CLAIMED", "RESOLVED", "BLOCKED"]
    action: ReconciliationAction
    requested_by: ReconciliationRequester
    result: ReconciliationResult | None
    result_code: str | None
    attempts: int
    available_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    @classmethod
    def from_view(cls, view: RuntimeReconciliationView) -> Self:
        return cls(
            deployment_id=view.deployment_id,
            state=view.state,
            action=view.action,
            requested_by=view.requested_by,
            result=view.result,
            result_code=view.result_code,
            attempts=view.attempts,
            available_at=view.available_at,
            updated_at=view.updated_at,
            completed_at=view.completed_at,
        )
