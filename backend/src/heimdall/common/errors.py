from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


@dataclass(slots=True)
class AppError(Exception):
    status: int
    code: str
    message: str


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, error: RequestValidationError) -> JSONResponse:
        violations = [
            {
                "path": ".".join(str(part) for part in item["loc"] if part != "body"),
                "code": item["type"],
            }
            for item in error.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "type": "urn:heimdall:problem:invalid-request",
                "title": "INVALID_REQUEST",
                "status": 422,
                "code": "INVALID_REQUEST",
                "message": "Request validation failed",
                "violations": violations,
            },
            headers={"Cache-Control": "no-store"},
            media_type="application/problem+json",
        )

    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, error: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status,
            content={
                "type": f"urn:heimdall:problem:{error.code.lower().replace('_', '-')}",
                "title": error.code,
                "status": error.status,
                "code": error.code,
                "message": error.message,
            },
            headers={"Cache-Control": "no-store"},
            media_type="application/problem+json",
        )
