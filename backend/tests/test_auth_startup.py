from __future__ import annotations

import hmac
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from heimdall.auth.secrets import AuthSecretError, initialize_admin_secrets
from heimdall.auth.service import (
    LOCAL_SESSION_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    AdminAuthService,
)
from heimdall.config import Settings
from heimdall.main import create_app


def test_create_app_loads_validated_auth_files_and_installs_secure_session_middleware(
    tmp_path: Path,
) -> None:
    root = tmp_path / "auth"
    initialize_admin_secrets(root, "startup-password-canary", "startup-password-canary")

    app = create_app(
        replace(
            Settings.from_environment(),
            auth_secret_root=root,
            auth_cookie_secure=True,
        )
    )

    assert isinstance(app.state.auth, AdminAuthService)
    middleware = next(item for item in app.user_middleware if item.cls is SessionMiddleware)
    expected_key = (root / "session-signing.key").read_text(encoding="utf-8")
    key_matches = hmac.compare_digest(middleware.kwargs["secret_key"], expected_key)
    assert key_matches is True
    non_secret_options = {
        key: value for key, value in middleware.kwargs.items() if key != "secret_key"
    }
    assert non_secret_options == {
        "session_cookie": SESSION_COOKIE_NAME,
        "max_age": SESSION_MAX_AGE_SECONDS,
        "path": "/",
        "same_site": "strict",
        "https_only": True,
        "domain": None,
    }


def test_create_app_fails_before_database_startup_when_auth_files_are_missing(
    tmp_path: Path,
) -> None:
    settings = replace(Settings.from_environment(), auth_secret_root=tmp_path / "missing-auth")

    with pytest.raises(AuthSecretError, match="missing"):
        create_app(settings)


def test_explicit_local_http_mode_uses_accepted_cookie_and_preserves_session(
    tmp_path: Path,
) -> None:
    root = tmp_path / "auth"
    password = "local-http-password-canary"
    initialize_admin_secrets(root, password, password)
    settings = replace(
        Settings.from_environment(),
        auth_secret_root=root,
        auth_cookie_secure=False,
    )
    app = create_app(settings)
    app.state.projects = type("ProjectListStub", (), {"list": lambda _: []})()
    client = TestClient(app, base_url="http://localhost")

    login = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": password},
    )

    assert login.status_code == 200
    set_cookie = login.headers["set-cookie"].lower()
    assert set_cookie.startswith(f"{LOCAL_SESSION_COOKIE_NAME}=")
    assert "secure" not in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie
    assert "path=/" in set_cookie
    assert "domain=" not in set_cookie
    assert client.get("/api/auth/session").status_code == 200
    assert client.get("/api/projects").status_code == 200

    csrf_token = login.json()["csrfToken"]
    logout = client.post(
        "/api/auth/logout",
        headers={"X-CSRF-Token": csrf_token},
    )

    assert logout.status_code == 200
    assert client.get("/api/auth/session").status_code == 401


def test_local_http_cookie_cannot_be_replayed_as_secure_cookie(tmp_path: Path) -> None:
    root = tmp_path / "auth"
    password = "cross-mode-password-canary"
    initialize_admin_secrets(root, password, password)
    base_settings = replace(Settings.from_environment(), auth_secret_root=root)
    local_client = TestClient(
        create_app(replace(base_settings, auth_cookie_secure=False)),
        base_url="http://localhost",
    )
    secure_client = TestClient(
        create_app(replace(base_settings, auth_cookie_secure=True)),
        base_url="https://heimdall.localhost",
    )
    login = local_client.post(
        "/api/auth/login",
        json={"username": "admin", "password": password},
    )
    local_cookie = local_client.cookies.get(LOCAL_SESSION_COOKIE_NAME)
    assert login.status_code == 200
    assert local_cookie is not None

    response = secure_client.get(
        "/api/auth/session",
        headers={"Cookie": f"{SESSION_COOKIE_NAME}={local_cookie}"},
    )

    assert response.status_code == 401
