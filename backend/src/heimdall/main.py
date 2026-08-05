from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI

from heimdall.api import router
from heimdall.common.errors import install_error_handlers
from heimdall.config import Settings
from heimdall.database import Database
from heimdall.deployments.repository import PostgresDeploymentRepository
from heimdall.deployments.service import DeploymentService
from heimdall.git.client import GitClient
from heimdall.project_database.provisioner import PostgresProjectDatabaseProvisioner
from heimdall.project_database.repository import PostgresProjectDatabaseRepository
from heimdall.project_database.service import ProjectDatabaseService
from heimdall.projects.repository import PostgresProjectRepository
from heimdall.projects.service import ProjectService
from heimdall.runtime.reconciliation_repository import PostgresRuntimeReconciliationRepository
from heimdall.runtime.reconciliation_service import RuntimeReconciliationService
from heimdall.runtime.repository import PostgresRuntimeRepository
from heimdall.runtime.status import RuntimeStatusService
from heimdall.secrets.store import FileSecretStore


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or Settings.from_environment()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database = Database(app_settings.database_url)
        database.open()
        git = GitClient(
            executable=app_settings.git_executable,
            timeout_seconds=app_settings.git_timeout_seconds,
            recent_commit_limit=app_settings.recent_commit_limit,
        )
        secret_store = FileSecretStore(app_settings.runtime_root)
        projects = ProjectService(PostgresProjectRepository(database), git, secret_store)
        provisioner = (
            PostgresProjectDatabaseProvisioner(app_settings.project_database_admin_url)
            if app_settings.project_database_enabled
            and app_settings.project_database_admin_url is not None
            else None
        )
        project_databases = ProjectDatabaseService(
            PostgresProjectDatabaseRepository(database),
            projects,
            secret_store,
            provisioner,
            app_settings.project_database_runtime_host,
            app_settings.project_database_runtime_port,
        )
        deployment_repository = PostgresDeploymentRepository(database)
        deployments = DeploymentService(deployment_repository, projects, project_databases)
        runtime_status = RuntimeStatusService(PostgresRuntimeRepository(database), projects)
        runtime_reconciliations = RuntimeReconciliationService(
            PostgresRuntimeReconciliationRepository(database),
            deployments,
            timedelta(hours=app_settings.runtime_retention_hours),
        )
        app.state.projects = projects
        app.state.project_databases = project_databases
        app.state.deployments = deployments
        app.state.runtime_status = runtime_status
        app.state.runtime_reconciliations = runtime_reconciliations
        yield
        database.close()

    app = FastAPI(title="Heimdall API", version="0.1.0", lifespan=lifespan)
    install_error_handlers(app)
    app.include_router(router, prefix="/api")
    return app


app = create_app()
