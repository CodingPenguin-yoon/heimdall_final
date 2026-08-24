from __future__ import annotations

from uuid import UUID

from heimdall.common.errors import AppError
from heimdall.projects.service import ProjectService
from heimdall.public_routes.models import (
    PublicRoute,
    PublicRouteConflictError,
    PublicRouteNotFoundError,
)
from heimdall.public_routes.repository import PublicRouteRepository
from heimdall.public_routes.schemas import PublicRouteUpdate


class PublicRouteService:
    def __init__(
        self,
        repository: PublicRouteRepository,
        projects: ProjectService,
        deployment_base_domain: str,
        reserved_subdomains: tuple[str, ...],
    ) -> None:
        self._repository = repository
        self._projects = projects
        self._deployment_base_domain = deployment_base_domain
        self._reserved_subdomains = frozenset(reserved_subdomains)

    def get(self, project_id: UUID) -> PublicRoute:
        self._projects.get(project_id)
        try:
            return self._repository.get(project_id)
        except PublicRouteNotFoundError as error:
            raise AppError(
                404,
                "PUBLIC_ROUTE_NOT_FOUND",
                "The project does not have a public hostname",
            ) from error

    def set(self, project_id: UUID, request: PublicRouteUpdate) -> PublicRoute:
        self._projects.get(project_id)
        if request.subdomain in self._reserved_subdomains:
            raise AppError(
                422,
                "PUBLIC_SUBDOMAIN_RESERVED",
                "The requested public subdomain is reserved",
            )
        hostname = f"{request.subdomain}.{self._deployment_base_domain}"
        if len(hostname) > 253:
            raise AppError(
                422,
                "PUBLIC_HOSTNAME_TOO_LONG",
                "The requested public hostname is too long",
            )
        try:
            return self._repository.set_enabled(project_id, request.subdomain, hostname)
        except PublicRouteConflictError as error:
            raise AppError(
                409,
                "PUBLIC_HOSTNAME_CONFLICT",
                "The requested public hostname is already reserved",
            ) from error

    def disable(self, project_id: UUID) -> PublicRoute:
        self._projects.get(project_id)
        try:
            return self._repository.disable(project_id)
        except PublicRouteNotFoundError as error:
            raise AppError(
                404,
                "PUBLIC_ROUTE_NOT_FOUND",
                "The project does not have a public hostname",
            ) from error

    def wake_pending_for_runtime(self, project_id: UUID) -> None:
        self._repository.wake_pending(project_id)
