from fastapi import APIRouter, Depends

from heimdall.auth.router import require_admin_request
from heimdall.auth.router import router as auth_router
from heimdall.deployments.router import router as deployments_router
from heimdall.project_database.router import router as project_database_router
from heimdall.projects.router import router as projects_router
from heimdall.public_routes.router import router as public_routes_router
from heimdall.runtime.api import router as runtime_router

router = APIRouter()
protected_router = APIRouter(dependencies=[Depends(require_admin_request)])
protected_router.include_router(projects_router, prefix="/projects", tags=["projects"])
protected_router.include_router(
    public_routes_router,
    prefix="/projects",
    tags=["public-routes"],
)
protected_router.include_router(deployments_router, tags=["deployments"])
protected_router.include_router(project_database_router, tags=["database"])
protected_router.include_router(runtime_router, tags=["runtime"])
router.include_router(auth_router, prefix="/auth", tags=["auth"])
router.include_router(protected_router)


@router.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}
