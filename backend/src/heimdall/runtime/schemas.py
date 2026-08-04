from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from heimdall.common.api_model import ApiModel
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
