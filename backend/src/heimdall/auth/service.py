from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

from heimdall.auth.secrets import AdminSecrets
from heimdall.common.errors import AppError

ADMIN_USERNAME = "admin"
SESSION_MAX_AGE_SECONDS = 8 * 60 * 60
SESSION_COOKIE_NAME = "__Host-heimdall-session"
LOCAL_SESSION_COOKIE_NAME = "heimdall-local-session"
SESSION_KEYS = frozenset({"username", "expires_at", "csrf_token", "credential_revision"})
CSRF_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
CREDENTIAL_REVISION = re.compile(r"^[a-f0-9]{64}$")
_LOCAL_SESSION_SIGNING_CONTEXT = b"heimdall-local-http-session-v1"


def session_signing_key(signing_key: str, *, secure: bool) -> str:
    if secure:
        return signing_key
    return hmac.new(
        signing_key.encode("utf-8"),
        _LOCAL_SESSION_SIGNING_CONTEXT,
        hashlib.sha256,
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class AdminSession:
    username: str
    expires_at: int
    csrf_token: str
    credential_revision: str

    def payload(self) -> dict[str, str | int]:
        return {
            "username": self.username,
            "expires_at": self.expires_at,
            "csrf_token": self.csrf_token,
            "credential_revision": self.credential_revision,
        }


class AdminAuthService:
    def __init__(
        self,
        admin_secrets: AdminSecrets,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._password_hash = admin_secrets.password_hash
        self._credential_revision = admin_secrets.credential_revision
        self._clock = clock
        self._password_hasher = PasswordHasher()

    def login(self, username: str, password: str) -> AdminSession:
        password_valid = False
        try:
            password_valid = self._password_hasher.verify(self._password_hash, password)
        except VerifyMismatchError:
            pass
        except VerificationError as error:
            raise RuntimeError("administrator password hash could not be verified") from error

        username_valid = secrets.compare_digest(
            username.encode("utf-8"),
            ADMIN_USERNAME.encode("ascii"),
        )
        if not password_valid or not username_valid:
            raise AppError(401, "INVALID_CREDENTIALS", "Invalid username or password")

        now = int(self._clock())
        return AdminSession(
            username=ADMIN_USERNAME,
            expires_at=now + SESSION_MAX_AGE_SECONDS,
            csrf_token=secrets.token_urlsafe(32),
            credential_revision=self._credential_revision,
        )

    def validate_session(self, payload: Mapping[str, Any]) -> AdminSession:
        if set(payload) != SESSION_KEYS:
            raise _authentication_required()
        username = payload.get("username")
        expires_at = payload.get("expires_at")
        csrf_token = payload.get("csrf_token")
        credential_revision = payload.get("credential_revision")
        valid_types = (
            type(username) is str
            and type(expires_at) is int
            and type(csrf_token) is str
            and type(credential_revision) is str
        )
        if not valid_types:
            raise _authentication_required()
        now = int(self._clock())
        if (
            username != ADMIN_USERNAME
            or expires_at <= now
            or expires_at > now + SESSION_MAX_AGE_SECONDS
        ):
            raise _authentication_required()
        if CSRF_TOKEN.fullmatch(csrf_token) is None:
            raise _authentication_required()
        if CREDENTIAL_REVISION.fullmatch(credential_revision) is None:
            raise _authentication_required()
        if not secrets.compare_digest(
            credential_revision.encode("utf-8"),
            self._credential_revision.encode("ascii"),
        ):
            raise _authentication_required()
        return AdminSession(username, expires_at, csrf_token, credential_revision)


def _authentication_required() -> AppError:
    return AppError(401, "AUTHENTICATION_REQUIRED", "Authentication is required")
