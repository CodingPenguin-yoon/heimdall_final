from __future__ import annotations

from uuid import UUID


def project_gateway_name(project_id: UUID) -> str:
    return f"hm-p{project_id.hex[:12]}-gateway"


def project_gateway_alias(project_id: UUID) -> str:
    return project_gateway_name(project_id)
