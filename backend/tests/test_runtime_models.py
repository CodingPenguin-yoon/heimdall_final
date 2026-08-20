from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from heimdall.deployments.models import (
    Deployment,
    DeploymentSource,
    DeploymentStatus,
)
from heimdall.runtime.models import RuntimeConfigurationError, RuntimeDeployment


def runtime_deployment() -> Deployment:
    now = datetime.now(UTC)
    return Deployment(
        id=uuid4(),
        project_id=uuid4(),
        source_type=DeploymentSource.MAIN_HEAD,
        requested_commit_sha=None,
        resolved_commit_sha="a" * 40,
        config_version=1,
        config_snapshot={
            "services": [
                {
                    "name": "api",
                    "build": {"context": ".", "dockerfile": "Dockerfile"},
                    "internalPort": 8000,
                    "healthPath": "/health",
                    "projectDatabaseAccess": True,
                    "environment": [
                        {"name": "APP_ENV", "kind": "PLAIN", "value": "production"},
                        {
                            "name": "JWT_SECRET",
                            "kind": "SECRET",
                            "secretReference": "projects/p1/environment/api/jwt/v1.secret",
                            "secretVersion": 1,
                            "secretFingerprint": "a" * 64,
                        },
                    ],
                }
            ],
            "routes": [{"path": "/", "service": "api"}],
            "managedDatabase": {
                "host": "managed-db.internal",
                "port": 5432,
                "databaseName": "hd_database",
                "username": "hd_role",
                "schemaName": "app",
                "credentialReference": "projects/p1/database/r1/credentials/v1.secret",
                "credentialVersion": 1,
                "credentialFingerprint": "b" * 64,
            },
        },
        status=DeploymentStatus.PREPARING,
        failure_stage=None,
        failure_code=None,
        created_at=now,
        updated_at=now,
        terminal_at=None,
    )


def test_runtime_snapshot_separates_plain_values_and_secret_file_contract() -> None:
    runtime = RuntimeDeployment.from_deployment(runtime_deployment())

    service = runtime.services[0]
    assert service.environment[0].value == "production"
    assert service.secrets[0].container_path == "/run/secrets/heimdall/environment/jwt_secret"
    assert runtime.database is not None
    assert runtime.database.container_path == "/run/secrets/heimdall/project-database-password"


def test_runtime_snapshot_requires_database_metadata_for_database_service() -> None:
    item = runtime_deployment()
    item.config_snapshot.pop("managedDatabase")

    with pytest.raises(RuntimeConfigurationError, match="database metadata"):
        RuntimeDeployment.from_deployment(item)


def test_runtime_snapshot_rejects_unsafe_build_path_even_if_control_data_is_corrupt() -> None:
    item = runtime_deployment()
    snapshot = deepcopy(item.config_snapshot)
    snapshot["services"][0]["build"]["context"] = "../outside"
    object.__setattr__(item, "config_snapshot", snapshot)

    with pytest.raises(RuntimeConfigurationError, match="canonical relative"):
        RuntimeDeployment.from_deployment(item)
