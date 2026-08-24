from __future__ import annotations

import hmac
from dataclasses import replace
from pathlib import Path

import pytest
from starlette.middleware.sessions import SessionMiddleware

from heimdall.auth.secrets import AuthSecretError, initialize_admin_secrets
from heimdall.auth.service import SESSION_COOKIE_NAME, SESSION_MAX_AGE_SECONDS, AdminAuthService
from heimdall.config import Settings
from heimdall.main import create_app


def test_create_app_loads_validated_auth_files_and_installs_secure_session_middleware(
    tmp_path: Path,
) -> None:
    root = tmp_path / "auth"
    initialize_admin_secrets(root, "startup-password-canary", "startup-password-canary")

    app = create_app(replace(Settings.from_environment(), auth_secret_root=root))

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
