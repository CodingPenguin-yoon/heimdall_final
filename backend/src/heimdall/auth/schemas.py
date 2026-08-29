from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import Field

from heimdall.auth.secrets import MAXIMUM_PASSWORD_LENGTH
from heimdall.auth.service import AdminSession
from heimdall.common.api_model import ApiModel


class AdminLogin(ApiModel):
    username: Annotated[str, Field(min_length=1, max_length=64)]
    password: Annotated[str, Field(min_length=1, max_length=MAXIMUM_PASSWORD_LENGTH)]


class AdminSessionRead(ApiModel):
    username: str
    csrf_token: str
    expires_at: datetime

    @classmethod
    def from_session(cls, session: AdminSession) -> AdminSessionRead:
        return cls(
            username=session.username,
            csrf_token=session.csrf_token,
            expires_at=datetime.fromtimestamp(session.expires_at, UTC),
        )


class AdminLogoutRead(ApiModel):
    logged_out: bool = True
