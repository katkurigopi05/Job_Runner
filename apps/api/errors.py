"""The shared error envelope: {"error": {"code", "message"}}. CLAUDE.md §10."""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

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


#: Which envelope code describes a status Starlette raised on its own.
#:
#: Needed because those never pass through `ApiError`: an unmatched path, a
#: wrong method, a malformed path parameter. Without this, a 404 for "no such
#: application" returned the envelope while a 404 for "no such route" returned
#: FastAPI's `{"detail": "Not Found"}` — two shapes for one status, and §10
#: promises one.
CODE_BY_STATUS: dict[int, ErrorCode] = {
    status.HTTP_400_BAD_REQUEST: ErrorCode.INVALID_REQUEST,
    status.HTTP_401_UNAUTHORIZED: ErrorCode.UNAUTHORIZED,
    status.HTTP_404_NOT_FOUND: ErrorCode.NOT_FOUND,
    status.HTTP_405_METHOD_NOT_ALLOWED: ErrorCode.INVALID_REQUEST,
    status.HTTP_409_CONFLICT: ErrorCode.INVALID_STATE,
    status.HTTP_429_TOO_MANY_REQUESTS: ErrorCode.RATE_LIMITED,
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

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Starlette's own errors, in this API's envelope.

        The status is taken from the exception rather than from
        `STATUS_BY_CODE`, because here the status is the known fact and the
        code is derived from it — the reverse of `ApiError`. Mapping it back
        through the table would rewrite a 405 into a 400.

        Anything unmapped keeps its status and reports `internal_error`, which
        is honest: the envelope has no code for it.
        """
        code = CODE_BY_STATUS.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": code.value, "message": str(exc.detail)}},
            headers=getattr(exc, "headers", None),
        )

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
