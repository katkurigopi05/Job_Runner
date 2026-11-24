"""An exception nothing mapped still answers in the shared envelope (CLAUDE.md §10).

Before the catch-all, an unmapped exception reached Starlette's own 500, a
plain-text "Internal Server Error". The dashboard reads `error.message` and,
finding none, said only "The assistant is unavailable."
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apps.api.errors import register_error_handlers
from apps.api.middleware import TraceContextMiddleware
from packages.core.trace import TRACE_HEADER

#: Stands in for what an exception's own text can carry: a Kaggle session URL
#: holds a credential in its path, and a parse error can quote a résumé.
SECRET = "https://kaggle.example/k/1/sekret-token/proxy"


def _crashing_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(TraceContextMiddleware)
    register_error_handlers(app)

    @app.post("/boom")
    async def boom() -> None:
        raise RuntimeError(f"could not reach {SECRET}")

    return app


async def _post_boom() -> tuple[int, dict[str, object], dict[str, str]]:
    # Starlette re-raises after answering so the server can log the
    # traceback; the transport must not turn that into a test failure.
    transport = ASGITransport(app=_crashing_app(), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.post("/boom")
    return response.status_code, response.json(), dict(response.headers)


@pytest.mark.asyncio
async def test_an_unmapped_exception_answers_in_the_envelope() -> None:
    # Arrange / Act
    status, body, _ = await _post_boom()

    # Assert
    assert status == 500
    error = body["error"]
    assert isinstance(error, dict)
    assert error["code"] == "internal_error"
    assert "RuntimeError" in error["message"]


@pytest.mark.asyncio
async def test_the_message_never_repeats_the_exception_text() -> None:
    _, body, _ = await _post_boom()

    assert "sekret-token" not in str(body)
    assert "kaggle.example" not in str(body)


@pytest.mark.asyncio
async def test_the_message_names_the_trace_the_terminal_logs() -> None:
    _, body, headers = await _post_boom()

    trace_id = headers.get(TRACE_HEADER.lower())
    assert trace_id
    error = body["error"]
    assert isinstance(error, dict)
    assert trace_id in error["message"]
