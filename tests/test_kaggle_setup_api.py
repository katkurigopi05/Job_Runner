"""The dashboard starts setup once, polls progress, and never exposes credentials."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from apps.api.errors import register_error_handlers
from apps.api.middleware import LocalhostOnlyMiddleware
from apps.api.routers import kaggle as api
from packages.llm.kaggle import Connection, ConnectionFailure, KaggleStatus
from packages.llm.kaggle_connection import save_connection

SERVER = "https://kkb-production.jupyter-proxy.kaggle.net/k/123/synthetic-credential/proxy"
KERNEL = UUID("00000000-0000-4000-8000-000000000001")


@pytest.fixture
async def dashboard(tmp_path, monkeypatch):
    path = tmp_path / "connection.json"
    save_connection(Connection(server_url=SecretStr(SERVER), kernel_id=KERNEL), path)
    monkeypatch.setattr(api, "connection_path", lambda: path)
    monkeypatch.setattr(api, "_setup_task", None)
    monkeypatch.setattr(api, "_setup_status", api.SetupStatus())
    monkeypatch.setattr(api.kaggle_setup, "Session", lambda url: url)
    monkeypatch.setattr(
        api.KaggleProvider,
        "health",
        AsyncMock(return_value=KaggleStatus(state="ready", detail="Ready")),
    )
    app = FastAPI()
    app.add_middleware(LocalhostOnlyMiddleware)
    register_error_handlers(app)
    app.include_router(api.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        yield client, path
    if api._setup_task is not None:
        await asyncio.wait_for(api._setup_task, timeout=5)


async def test_button_starts_setup_once_and_reports_progress(dashboard, monkeypatch, caplog):
    client, path = dashboard
    release = threading.Event()
    started = threading.Event()
    calls = []

    async def setup(session, saved, destination, *, say):
        calls.append((session, saved, destination))
        say("Installing the helper...")
        say("  Installed synthetic-credential")  # raw kernel output is withheld
        started.set()
        assert release.wait(timeout=5)
        return "ready"

    monkeypatch.setattr(api.kaggle_setup, "setup", setup)
    initial = await client.post("/chat/kaggle/setup")
    assert initial.status_code == 202 and initial.json()["running"]
    try:
        assert await asyncio.to_thread(started.wait, 2)
        progress = await client.get("/chat/kaggle/setup")
        assert progress.json()["running"]
        assert progress.json()["detail"] == "Installing the helper..."
        duplicate = await client.post("/chat/kaggle/setup")
        assert duplicate.status_code == 202
        reconnect = await client.post("/chat/kaggle/connect", json={"server_url": SERVER})
        assert reconnect.status_code == 409
        assert len(calls) == 1
    finally:
        release.set()
        await asyncio.wait_for(api._setup_task, timeout=5)
    complete = await client.get("/chat/kaggle/setup")
    assert not complete.json()["running"]
    assert complete.json()["connection"]["state"] == "ready"
    assert calls[0][2] == path
    assert calls[0][1].kernel_id == KERNEL
    assert "synthetic-credential" not in initial.text + progress.text + complete.text + caplog.text


@pytest.mark.parametrize("failure", [RuntimeError(SERVER), ConnectionFailure("expired")])
async def test_failed_setup_is_private_and_can_be_retried(dashboard, monkeypatch, failure):
    client, path = dashboard
    before = path.read_bytes()
    setup = AsyncMock(side_effect=failure)
    monkeypatch.setattr(api.kaggle_setup, "setup", setup)
    await client.post("/chat/kaggle/setup")
    await asyncio.wait_for(api._setup_task, timeout=5)
    result = await client.get("/chat/kaggle/setup")
    assert not result.json()["running"]
    assert result.json()["error"]
    assert "synthetic-credential" not in result.text
    assert path.read_bytes() == before
    setup.side_effect = None
    setup.return_value = "ready"
    assert (await client.post("/chat/kaggle/setup")).status_code == 202
    await asyncio.wait_for(api._setup_task, timeout=5)
    assert (await client.get("/chat/kaggle/setup")).json()["connection"]["state"] == "ready"


async def test_setup_without_saved_connection_does_not_run(dashboard, monkeypatch):
    client, path = dashboard
    path.unlink()
    setup = AsyncMock()
    monkeypatch.setattr(api.kaggle_setup, "setup", setup)
    result = await client.post("/chat/kaggle/setup")
    assert result.status_code == 400
    setup.assert_not_called()


async def test_foreign_origin_cannot_start_setup(dashboard, monkeypatch):
    client, _ = dashboard
    setup = AsyncMock()
    monkeypatch.setattr(api.kaggle_setup, "setup", setup)
    result = await client.post("/chat/kaggle/setup", headers={"Origin": "https://example.test"})
    assert result.status_code == 401
    setup.assert_not_called()
