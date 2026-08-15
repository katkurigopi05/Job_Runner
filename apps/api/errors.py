"""The shared error envelope: {"error": {"code", "message"}}. CLAUDE.md §10."""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from packages.core.enums import ErrorCode
from packages.core.state import InvalidTransitionError

STATUS_BY_CODE: dict[ErrorCode, int] = {
    ErrorCode.UNAUTHORIZED: status.HTTP_401_UNAUTHORIZED,
    ErrorCode.INVALID_REQUEST: status.HTTP_400_BAD_REQUEST,
    ErrorCode.NOT_FOUND: status.HTTP_404_NOT_FOUND,
    ErrorCode.RATE_LIMITED: status.HTTP_429_TOO_MANY_REQUESTS,
    ErrorCode.DUPLICATE_APPLICATION: status.HTTP_409_CONFLICT,
    ErrorCode.INVALID_STATE: status.HTTP_409_CONFLICT,
    ErrorCode.INTERNAL_ERROR: status.HTTP_500_INTERNAL_SERVER_ERROR,
}


class ApiError(Exception):
    """Raise anywhere in a handler to produce the shared envelope."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _envelope(code: ErrorCode, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=STATUS_BY_CODE[code],
        content={"error": {"code": code.value, "message": message}},
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return _envelope(exc.code, exc.message)

    @app.exception_handler(InvalidTransitionError)
    async def _invalid_transition(_: Request, exc: InvalidTransitionError) -> JSONResponse:
        return _envelope(ErrorCode.INVALID_STATE, str(exc))

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Flatten pydantic's structure into one line — the envelope has no
        # field for per-field detail and inventing one would fork the contract.
        parts = [
            f"{'.'.join(str(p) for p in err['loc'][1:])}: {err['msg']}" for err in exc.errors()
        ]
        return _envelope(ErrorCode.INVALID_REQUEST, "; ".join(parts) or "invalid request")

    @app.exception_handler(IntegrityError)
    async def _integrity(_: Request, exc: IntegrityError) -> JSONResponse:
        constraint = getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)
        if constraint == "uq_applications_candidate_url":
            return _envelope(
                ErrorCode.DUPLICATE_APPLICATION,
                "an application for this candidate and url already exists",
            )
        return _envelope(ErrorCode.INVALID_REQUEST, "constraint violation")
