from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from starlette.middleware.sessions import SessionMiddleware

from heimdall.api import router as api_router
from heimdall.auth.secrets import AdminSecrets
from heimdall.auth.service import (
    SESSION_COOKIE_NAME,
    SESSION_KEYS,
    SESSION_MAX_AGE_SECONDS,
    AdminAuthService,
)
from heimdall.common.errors import install_error_handlers
from heimdall.projects.models import Project, ProjectStatus

NOW = 1_800_000_000
PASSWORD = "correct-password-canary"
SIGNING_KEY = "s" * 64
HTTPS_BASE_URL = "https://control.example.test"


class MutableClock:
    def __init__(self, value: float = NOW) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class ProjectSpy:
    def __init__(self) -> None:
        self.list_calls = 0
        self.create_calls = 0

    def list(self) -> list[Project]:
        self.list_calls += 1
        return []

    def create(self, payload) -> Project:
        self.create_calls += 1
        now = datetime.now(UTC)
        return Project(
            id=uuid4(),
            name=payload.name,
            repository_url=payload.repository_url,
            branch="main",
            status=ProjectStatus.DRAFT,
            config_version=0,
            deployment_config=None,
            created_at=now,
            updated_at=now,
        )


def admin_secrets(
    password: str = PASSWORD,
    signing_key: str = SIGNING_KEY,
) -> AdminSecrets:
    password_hash = PasswordHasher().hash(password)
    return AdminSecrets(
        password_hash=password_hash,
        signing_key=signing_key,
        credential_revision=hashlib.sha256(password_hash.encode("utf-8")).hexdigest(),
    )


def auth_app(
    secrets: AdminSecrets,
    clock: MutableClock | None = None,
) -> FastAPI:
    app = FastAPI()
    app.state.auth = AdminAuthService(secrets, clock=clock or MutableClock())
    app.add_middleware(
        SessionMiddleware,
        secret_key=secrets.signing_key,
        session_cookie=SESSION_COOKIE_NAME,
        max_age=SESSION_MAX_AGE_SECONDS,
        path="/",
        same_site="strict",
        https_only=True,
        domain=None,
    )
    install_error_handlers(app)
    app.include_router(api_router, prefix="/api")
    return app


def login(client: TestClient, password: str = PASSWORD):
    return client.post(
        "/api/auth/login",
        json={"username": "admin", "password": password},
    )


def decode_cookie(cookie: str, signing_key: str) -> dict:
    encoded = TimestampSigner(signing_key).unsign(
        cookie.encode("utf-8"),
        max_age=SESSION_MAX_AGE_SECONDS,
    )
    return json.loads(base64.b64decode(encoded))


def encode_cookie(payload: dict, signing_key: str) -> str:
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8"))
    return TimestampSigner(signing_key).sign(encoded).decode("utf-8")


def test_login_sets_exact_secure_session_and_session_endpoint_returns_camel_case() -> None:
    secrets = admin_secrets()
    client = TestClient(auth_app(secrets), base_url=HTTPS_BASE_URL)

    response = login(client)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    expected_expiry = (
        datetime.fromtimestamp(NOW + SESSION_MAX_AGE_SECONDS, UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )
    assert response.json() == {
        "username": "admin",
        "csrfToken": response.json()["csrfToken"],
        "expiresAt": expected_expiry,
    }
    set_cookie = response.headers["set-cookie"].lower()
    assert "secure" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie
    assert "path=/" in set_cookie
    assert f"max-age={SESSION_MAX_AGE_SECONDS}" in set_cookie
    assert "domain=" not in set_cookie

    cookie = client.cookies.get(SESSION_COOKIE_NAME)
    payload = decode_cookie(cookie, secrets.signing_key)
    assert set(payload) == SESSION_KEYS
    assert payload == {
        "username": "admin",
        "expires_at": NOW + SESSION_MAX_AGE_SECONDS,
        "csrf_token": response.json()["csrfToken"],
        "credential_revision": secrets.credential_revision,
    }
    combined_output = response.text + response.headers["set-cookie"]
    password_exposed = PASSWORD in combined_output
    hash_exposed = secrets.password_hash in combined_output
    key_exposed = secrets.signing_key in combined_output
    assert password_exposed is False
    assert hash_exposed is False
    assert key_exposed is False

    checked = client.get("/api/auth/session")
    assert checked.status_code == 200
    assert checked.headers["cache-control"] == "no-store"
    assert checked.json() == response.json()


def test_wrong_username_and_password_are_identical_and_always_verify_password(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secrets = admin_secrets()
    app = auth_app(secrets)
    calls: list[tuple[str, str]] = []

    class RecordingHasher:
        def verify(self, password_hash: str, password: str) -> bool:
            calls.append((password_hash, password))
            return password == PASSWORD

    app.state.auth._password_hasher = RecordingHasher()
    client = TestClient(app, base_url=HTTPS_BASE_URL)

    wrong_username = client.post(
        "/api/auth/login",
        json={"username": "someone-else", "password": PASSWORD},
    )
    wrong_password = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "wrong-password-canary"},
    )

    assert wrong_username.status_code == 401
    assert wrong_password.status_code == 401
    assert wrong_username.json() == wrong_password.json()
    assert wrong_username.headers["cache-control"] == "no-store"
    assert wrong_password.headers["cache-control"] == "no-store"
    assert [password for _, password in calls] == [PASSWORD, "wrong-password-canary"]
    exposed = wrong_username.text + wrong_password.text + caplog.text
    password_exposed = PASSWORD in exposed or "wrong-password-canary" in exposed
    hash_exposed = secrets.password_hash in exposed
    assert password_exposed is False
    assert hash_exposed is False


def test_login_input_is_length_bounded_without_echoing_password() -> None:
    client = TestClient(auth_app(admin_secrets()), base_url=HTTPS_BASE_URL)
    password = "length-canary-" + "x" * 1024

    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": password},
    )

    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    password_exposed = password in response.text or "length-canary" in response.text
    assert password_exposed is False


def test_csrf_is_required_exactly_for_mutation_and_not_for_authenticated_get() -> None:
    app = auth_app(admin_secrets())
    projects = ProjectSpy()
    app.state.projects = projects
    client = TestClient(app, base_url=HTTPS_BASE_URL)
    csrf_token = login(client).json()["csrfToken"]
    body = {"name": "Example", "repositoryUrl": "https://github.com/example/project"}

    missing = client.post("/api/projects", json=body)
    wrong = client.post(
        "/api/projects",
        json=body,
        headers={"X-CSRF-Token": f"{csrf_token}-wrong"},
    )
    fetched = client.get("/api/projects")
    accepted = client.post(
        "/api/projects",
        json=body,
        headers={"X-CSRF-Token": csrf_token},
    )

    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert missing.headers["cache-control"] == "no-store"
    assert wrong.headers["cache-control"] == "no-store"
    assert fetched.status_code == 200
    assert accepted.status_code == 201
    assert projects.list_calls == 1
    assert projects.create_calls == 1


def test_logout_requires_csrf_clears_cookie_and_returns_json() -> None:
    client = TestClient(auth_app(admin_secrets()), base_url=HTTPS_BASE_URL)
    csrf_token = login(client).json()["csrfToken"]

    rejected = client.post("/api/auth/logout")
    response = client.post(
        "/api/auth/logout",
        headers={"X-CSRF-Token": csrf_token},
    )

    assert rejected.status_code == 403
    assert response.status_code == 200
    assert response.json() == {"loggedOut": True}
    assert response.headers["cache-control"] == "no-store"
    assert "expires=thu, 01 jan 1970" in response.headers["set-cookie"].lower()
    checked = client.get("/api/auth/session")
    assert checked.status_code == 401
    assert checked.headers["cache-control"] == "no-store"


def test_tampered_and_absolutely_expired_cookies_are_rejected() -> None:
    secrets = admin_secrets()
    clock = MutableClock()
    app = auth_app(secrets, clock)
    client = TestClient(app, base_url=HTTPS_BASE_URL)
    login(client)
    cookie = client.cookies.get(SESSION_COOKIE_NAME)
    tampered = ("A" if cookie[0] != "A" else "B") + cookie[1:]

    tampered_response = TestClient(app, base_url=HTTPS_BASE_URL).get(
        "/api/auth/session",
        headers={"Cookie": f"{SESSION_COOKIE_NAME}={tampered}"},
    )
    clock.value = NOW + SESSION_MAX_AGE_SECONDS
    expired_response = client.get("/api/auth/session")

    assert tampered_response.status_code == 401
    assert expired_response.status_code == 401
    assert tampered_response.headers["cache-control"] == "no-store"
    assert expired_response.headers["cache-control"] == "no-store"


def test_password_hash_and_signing_key_rotation_each_invalidate_old_session() -> None:
    original = admin_secrets()
    old_client = TestClient(auth_app(original), base_url=HTTPS_BASE_URL)
    login(old_client)
    cookie = old_client.cookies.get(SESSION_COOKIE_NAME)
    cookie_header = {"Cookie": f"{SESSION_COOKIE_NAME}={cookie}"}

    credential_rotated = admin_secrets(password="new-password", signing_key=original.signing_key)
    signing_rotated = AdminSecrets(
        password_hash=original.password_hash,
        signing_key="n" * 64,
        credential_revision=original.credential_revision,
    )
    old_credential = TestClient(auth_app(credential_rotated), base_url=HTTPS_BASE_URL).get(
        "/api/auth/session", headers=cookie_header
    )
    old_signing_key = TestClient(auth_app(signing_rotated), base_url=HTTPS_BASE_URL).get(
        "/api/auth/session", headers=cookie_header
    )

    assert old_credential.status_code == 401
    assert old_signing_key.status_code == 401


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.pop("csrf_token"),
        lambda payload: payload.update({"extra": "not-allowed"}),
        lambda payload: payload.update({"expires_at": NOW}),
        lambda payload: payload.update({"expires_at": NOW + SESSION_MAX_AGE_SECONDS + 1}),
        lambda payload: payload.update({"credential_revision": "0" * 64}),
        lambda payload: payload.update({"csrf_token": "not-a-token"}),
    ],
)
def test_malformed_signed_session_is_rejected_before_feature_service(
    mutate,
) -> None:
    secrets = admin_secrets()
    app = auth_app(secrets)
    projects = ProjectSpy()
    app.state.projects = projects
    payload = {
        "username": "admin",
        "expires_at": NOW + SESSION_MAX_AGE_SECONDS,
        "csrf_token": "c" * 43,
        "credential_revision": secrets.credential_revision,
    }
    mutate(payload)
    cookie = encode_cookie(payload, secrets.signing_key)

    response = TestClient(app, base_url=HTTPS_BASE_URL).get(
        "/api/projects",
        headers={"Cookie": f"{SESSION_COOKIE_NAME}={cookie}"},
    )

    assert response.status_code == 401
    assert projects.list_calls == 0


def test_every_management_api_route_defaults_to_unauthenticated() -> None:
    app = auth_app(admin_secrets())
    client = TestClient(app, base_url=HTTPS_BASE_URL)
    public_paths = {
        "/api/health",
        "/api/auth/login",
        "/api/auth/session",
        "/api/auth/logout",
    }
    checked: set[tuple[str, str]] = set()

    for path, operations in app.openapi()["paths"].items():
        if path in public_paths:
            continue
        concrete_path = re.sub(r"\{[^}]+\}", str(uuid4()), path)
        for method in operations:
            response = client.request(
                method.upper(),
                concrete_path,
                json={} if method.upper() in {"POST", "PUT", "PATCH"} else None,
            )
            assert response.status_code == 401, (method, path, response.text)
            checked.add((method.upper(), path))

    assert len(checked) >= 20
    assert ("GET", "/api/deployments/{deployment_id}/events/stream") in checked
    assert ("GET", "/api/deployments/{deployment_id}/service-logs/stream") in checked
    assert ("PUT", "/api/projects/{project_id}/public-route") in checked
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/auth/session").status_code == 401
    assert client.post("/api/auth/logout").status_code == 401


def test_unauthenticated_sse_handshake_never_opens_subscription() -> None:
    class Deployments:
        event_stream_opened = False
        log_stream_opened = False

        def open_event_stream(self, deployment_id, after):
            self.event_stream_opened = True
            raise AssertionError("event stream service must not be called")

        def open_service_log_stream(self, deployment_id, service_name):
            self.log_stream_opened = True
            raise AssertionError("log stream service must not be called")

    app = auth_app(admin_secrets())
    deployments = Deployments()
    app.state.deployments = deployments
    deployment_id = uuid4()
    client = TestClient(app, base_url=HTTPS_BASE_URL)

    events = client.get(f"/api/deployments/{deployment_id}/events/stream")
    logs = client.get(f"/api/deployments/{deployment_id}/service-logs/stream")

    assert events.status_code == 401
    assert logs.status_code == 401
    assert deployments.event_stream_opened is False
    assert deployments.log_stream_opened is False
