from uuid import UUID

from heimdall.projects.service import ProjectService
from heimdall.runtime.repository import ProjectRuntime, RuntimeRepository


class RuntimeStatusService:
    def __init__(self, repository: RuntimeRepository, projects: ProjectService) -> None:
        self._repository = repository
        self._projects = projects

    def get(self, project_id: UUID) -> ProjectRuntime | None:
        self._projects.get(project_id)
        return self._repository.get(project_id)
