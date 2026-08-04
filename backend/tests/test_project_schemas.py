import pytest
from pydantic import ValidationError

from heimdall.projects.schemas import ProjectSettingsUpdate


def valid_settings() -> dict:
    return {
        "expectedVersion": 0,
        "services": [
            {
                "name": "web",
                "build": {"context": "frontend", "dockerfile": "Dockerfile"},
                "internalPort": 3000,
                "healthPath": "/",
            },
            {
                "name": "api",
                "build": {"context": "backend", "dockerfile": "Dockerfile"},
                "internalPort": 8000,
                "healthPath": "/health",
            },
        ],
        "routes": [
            {"path": "/api", "service": "api"},
            {"path": "/", "service": "web"},
        ],
    }


def test_settings_accept_multi_service_routes() -> None:
    settings = ProjectSettingsUpdate.model_validate(valid_settings())

    assert [item.name for item in settings.services] == ["web", "api"]
    assert settings.snapshot()["routes"][0] == {"path": "/api", "service": "api"}


def test_settings_require_root_route() -> None:
    payload = valid_settings()
    payload["routes"] = [{"path": "/api", "service": "api"}]

    with pytest.raises(ValidationError, match="root path"):
        ProjectSettingsUpdate.model_validate(payload)


def test_settings_reject_unknown_route_service() -> None:
    payload = valid_settings()
    payload["routes"][0]["service"] = "missing"

    with pytest.raises(ValidationError, match="unknown services"):
        ProjectSettingsUpdate.model_validate(payload)


def test_settings_reject_repository_path_escape() -> None:
    payload = valid_settings()
    payload["services"][0]["build"]["context"] = "../frontend"

    with pytest.raises(ValidationError, match="repository-relative"):
        ProjectSettingsUpdate.model_validate(payload)


def test_settings_accept_plain_and_secret_environment() -> None:
    payload = valid_settings()
    payload["services"][1]["environment"] = [
        {"name": "APP_ENV", "kind": "PLAIN", "value": "production"},
        {"name": "JWT_SECRET", "kind": "SECRET", "value": "write-only"},
    ]
    payload["services"][1]["projectDatabaseAccess"] = True

    settings = ProjectSettingsUpdate.model_validate(payload)

    assert settings.services[1].project_database_access is True
    assert settings.services[1].environment[1].value == "write-only"


@pytest.mark.parametrize("name", ["DATABASE_HOST", "DATABASE_URL", "HEIMDALL_PROJECT_ID"])
def test_settings_reject_reserved_environment(name: str) -> None:
    payload = valid_settings()
    payload["services"][0]["environment"] = [{"name": name, "kind": "PLAIN", "value": "override"}]

    with pytest.raises(ValidationError, match="reserved"):
        ProjectSettingsUpdate.model_validate(payload)


def test_route_rejects_nginx_configuration_characters() -> None:
    payload = valid_settings()
    payload["routes"][0]["path"] = "/;return 200"

    with pytest.raises(ValidationError):
        ProjectSettingsUpdate.model_validate(payload)
