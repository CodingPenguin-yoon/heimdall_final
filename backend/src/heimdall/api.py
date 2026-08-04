from fastapi import APIRouter

from heimdall.deployments.router import router as deployments_router
from heimdall.project_database.router import router as project_database_router
from heimdall.projects.router import router as projects_router
from heimdall.runtime.api import router as runtime_router

router = APIRouter()
router.include_router(projects_router, prefix="/projects", tags=["projects"])
router.include_router(deployments_router, tags=["deployments"])
router.include_router(project_database_router, tags=["database"])
router.include_router(runtime_router, tags=["runtime"])


@router.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}
