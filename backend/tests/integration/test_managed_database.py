import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from conftest import FakeGit
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from test_project_schemas import valid_settings

from heimdall.database import Database
from heimdall.project_database.provisioner import PostgresProjectDatabaseProvisioner
from heimdall.project_database.repository import PostgresProjectDatabaseRepository
from heimdall.project_database.service import ProjectDatabaseService
from heimdall.projects.repository import PostgresProjectRepository
from heimdall.projects.schemas import ProjectCreate, ProjectSettingsUpdate
from heimdall.projects.service import ProjectService
from heimdall.secrets.store import FileSecretStore

CONTROL_URL = os.environ.get("HEIMDALL_TEST_CONTROL_DB_URL")
MANAGED_URL = os.environ.get("HEIMDALL_TEST_MANAGED_DB_ADMIN_URL")

pytestmark = pytest.mark.skipif(
    not CONTROL_URL or not MANAGED_URL,
    reason="managed PostgreSQL integration URLs are not configured",
)


def test_two_projects_receive_isolated_managed_databases(tmp_path: Path) -> None:
    assert CONTROL_URL is not None
    assert MANAGED_URL is not None
    control = Database(CONTROL_URL)
    control.open()
    try:
        project_repository = PostgresProjectRepository(control)
        projects = ProjectService(project_repository, FakeGit(), FileSecretStore(tmp_path))
        database_repository = PostgresProjectDatabaseRepository(control)
        databases = ProjectDatabaseService(
            database_repository,
            projects,
            FileSecretStore(tmp_path),
            PostgresProjectDatabaseProvisioner(MANAGED_URL),
            "managed-postgres",
            5432,
        )

        run_id = uuid4().hex
        first = _create_ready_project(projects, f"First-{run_id}", f"first-{run_id}")
        second = _create_ready_project(projects, f"Second-{run_id}", f"second-{run_id}")
        first_result = databases.provision(first.id)
        second_result = databases.provision(second.id)

        assert first_result.status == "ACTIVE"
        assert second_result.status == "ACTIVE"
        assert first_result.database_name != second_result.database_name
        assert first_result.username != second_result.username

        first_resource = database_repository.get_for_project(first.id)
        second_resource = database_repository.get_for_project(second.id)
        assert first_resource is not None
        assert second_resource is not None
        first_password = FileSecretStore(tmp_path).read(
            first_resource.credential_reference or "",
            first_resource.credential_fingerprint or "",
        )

        with psycopg.connect(
            _project_url(
                MANAGED_URL,
                first_resource.database_name,
                first_resource.role_name,
                first_password,
            )
        ) as connection:
            assert connection.execute("SELECT current_schema").fetchone()[0] == "app"

        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(
                _project_url(
                    MANAGED_URL,
                    second_resource.database_name,
                    first_resource.role_name,
                    first_password,
                )
            )

        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(
                _project_url(
                    MANAGED_URL,
                    "postgres",
                    first_resource.role_name,
                    first_password,
                )
            )
    finally:
        control.close()


def _create_ready_project(projects: ProjectService, name: str, repository: str):
    project = projects.create(
        ProjectCreate(
            name=name,
            repositoryUrl=f"https://github.com/example/{repository}",
        )
    )
    payload = valid_settings()
    payload["services"][1]["projectDatabaseAccess"] = True
    return projects.update_settings(project.id, ProjectSettingsUpdate.model_validate(payload))


def _project_url(admin_url: str, database: str, user: str, password: str) -> str:
    values = conninfo_to_dict(admin_url)
    values.update(
        {
            "dbname": database,
            "user": user,
            "password": password,
            "connect_timeout": "3",
        }
    )
    return make_conninfo(**values)
