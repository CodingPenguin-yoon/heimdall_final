from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from heimdall.api import router
from heimdall.auth.secrets import load_admin_secrets
from heimdall.auth.service import SESSION_COOKIE_NAME, SESSION_MAX_AGE_SECONDS, AdminAuthService
from heimdall.common.errors import install_error_handlers
from heimdall.config import Settings
from heimdall.database import Database
from heimdall.deployments.event_stream import PostgresDeploymentEventStreamGateway
from heimdall.deployments.repository import PostgresDeploymentRepository
from heimdall.deployments.service import DeploymentService
from heimdall.git.client import GitClient
from heimdall.project_database.provisioner import PostgresProjectDatabaseProvisioner
from heimdall.project_database.repository import PostgresProjectDatabaseRepository
from heimdall.project_database.service import ProjectDatabaseService
from heimdall.projects.repository import PostgresProjectRepository
from heimdall.projects.service import ProjectService
from heimdall.public_routes.repository import PostgresPublicRouteRepository
from heimdall.public_routes.service import PublicRouteService
from heimdall.runtime.log_broker import UnixServiceLogBrokerClient, service_log_socket_path
from heimdall.runtime.log_stream_broker import (
    UnixServiceLogStreamBrokerClient,
    service_log_stream_socket_path,
)
from heimdall.runtime.reconciliation_repository import PostgresRuntimeReconciliationRepository
from heimdall.runtime.reconciliation_service import RuntimeReconciliationService
from heimdall.runtime.repository import PostgresRuntimeRepository
from heimdall.runtime.status import RuntimeStatusService
from heimdall.secrets.store import FileSecretStore


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or Settings.from_environment()
    admin_secrets = load_admin_secrets(app_settings.auth_secret_root)
    auth = AdminAuthService(admin_secrets)

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
        deployments = DeploymentService(
            deployment_repository,
            projects,
            project_databases,
            UnixServiceLogBrokerClient(
                service_log_socket_path(app_settings.broker_socket_root),
                timeout_seconds=app_settings.service_log_broker_timeout_seconds,
            ),
            UnixServiceLogStreamBrokerClient(
                service_log_stream_socket_path(app_settings.broker_socket_root),
                handshake_timeout_seconds=app_settings.service_log_broker_timeout_seconds,
            ),
            PostgresDeploymentEventStreamGateway(database, deployment_repository),
        )
        runtime_status = RuntimeStatusService(PostgresRuntimeRepository(database), projects)
        runtime_reconciliations = RuntimeReconciliationService(
            PostgresRuntimeReconciliationRepository(database),
            deployments,
            timedelta(hours=app_settings.runtime_retention_hours),
        )
        public_routes = PublicRouteService(
            PostgresPublicRouteRepository(database),
            projects,
            app_settings.deployment_base_domain,
            app_settings.reserved_public_subdomains,
        )
        app.state.projects = projects
        app.state.project_databases = project_databases
        app.state.deployments = deployments
        app.state.runtime_status = runtime_status
        app.state.runtime_reconciliations = runtime_reconciliations
        app.state.public_routes = public_routes
        yield
        database.close()

    app = FastAPI(title="Heimdall API", version="0.1.0", lifespan=lifespan)
    app.state.auth = auth
    app.add_middleware(
        SessionMiddleware,
        secret_key=admin_secrets.signing_key,
        session_cookie=SESSION_COOKIE_NAME,
        max_age=SESSION_MAX_AGE_SECONDS,
        path="/",
        same_site="strict",
        https_only=True,
        domain=None,
    )
    install_error_handlers(app)
    app.include_router(router, prefix="/api")
    return app
