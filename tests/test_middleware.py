"""Localhost-only enforcement.

The API has no authentication and can submit real job applications, so the app
itself refuses non-loopback callers rather than trusting the launch command.
"""

from __future__ import annotations

import uuid

import pytest

from apps.api.middleware import _is_loopback


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "127.0.0.53", "localhost", None])
def test_loopback_is_allowed(host) -> None:
    assert _is_loopback(host)


@pytest.mark.parametrize("host", ["10.0.0.5", "192.168.1.20", "8.8.8.8", "0.0.0.0", "example.com"])
def test_remote_hosts_are_refused(host: str) -> None:
    assert not _is_loopback(host)


async def test_non_local_request_gets_the_error_envelope() -> None:
    from httpx import ASGITransport, AsyncClient

    from apps.api.main import app

    transport = ASGITransport(app=app, client=("10.0.0.5", 12345))
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        r = await client.get("/health")

    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"
    assert uuid.UUID(r.headers["X-Trace-ID"])


async def test_local_request_passes() -> None:
    from httpx import ASGITransport, AsyncClient

    from apps.api.main import app

    transport = ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        r = await client.get("/health")

    assert r.status_code == 200


async def test_trace_id_round_trips_on_the_response() -> None:
    from httpx import ASGITransport, AsyncClient

    from apps.api.main import app

    trace_id = str(uuid.uuid4())
    transport = ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.get("/", headers={"X-Trace-ID": trace_id})

    assert response.headers["X-Trace-ID"] == trace_id


async def test_an_untrusted_trace_value_is_replaced() -> None:
    from httpx import ASGITransport, AsyncClient

    from apps.api.main import app

    transport = ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.get("/", headers={"X-Trace-ID": "resume text, not an id"})

    assert uuid.UUID(response.headers["X-Trace-ID"])
    assert response.headers["X-Trace-ID"] != "resume text, not an id"


async def test_trace_context_reaches_the_request_handler() -> None:
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from apps.api.middleware import TraceContextMiddleware
    from packages.core.trace import current_trace_id

    traced = FastAPI()
    traced.add_middleware(TraceContextMiddleware)

    @traced.get("/")
    async def read_trace() -> dict[str, str | None]:
        return {"trace_id": current_trace_id()}

    trace_id = str(uuid.uuid4())
    async with AsyncClient(transport=ASGITransport(app=traced), base_url="http://test") as client:
        response = await client.get("/", headers={"X-Trace-ID": trace_id})

    assert response.json() == {"trace_id": trace_id}
