from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from heimdall.auth.schemas import AdminLogin, AdminLogoutRead, AdminSessionRead
from heimdall.auth.service import AdminAuthService, AdminSession
from heimdall.common.errors import AppError

router = APIRouter()
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def service(request: Request) -> AdminAuthService:
    return request.app.state.auth


def current_admin_session(request: Request) -> AdminSession:
    try:
        return service(request).validate_session(request.session)
    except AppError:
        request.session.clear()
        raise


def require_admin_request(request: Request) -> AdminSession:
    session = current_admin_session(request)
    if request.method in UNSAFE_METHODS:
        submitted = request.headers.getlist("x-csrf-token")
        if len(submitted) != 1 or not secrets.compare_digest(
            submitted[0].encode("utf-8"),
            session.csrf_token.encode("ascii"),
        ):
            raise AppError(403, "CSRF_TOKEN_INVALID", "CSRF token is missing or invalid")
    return session


@router.post("/login", response_model=AdminSessionRead)
def login(
    payload: AdminLogin,
    request: Request,
    response: Response,
) -> AdminSessionRead:
    session = service(request).login(payload.username, payload.password)
    request.session.clear()
    request.session.update(session.payload())
    response.headers["Cache-Control"] = "no-store"
    return AdminSessionRead.from_session(session)


@router.get("/session", response_model=AdminSessionRead)
def read_session(
    response: Response,
    session: Annotated[AdminSession, Depends(current_admin_session)],
) -> AdminSessionRead:
    response.headers["Cache-Control"] = "no-store"
    return AdminSessionRead.from_session(session)


@router.post("/logout", response_model=AdminLogoutRead)
def logout(
    request: Request,
    response: Response,
    _: Annotated[AdminSession, Depends(require_admin_request)],
) -> AdminLogoutRead:
    request.session.clear()
    response.headers["Cache-Control"] = "no-store"
    return AdminLogoutRead()
