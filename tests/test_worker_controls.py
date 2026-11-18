"""Tests for crawler pause/resume/stop and worker task-kind / concurrency controls."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

from httpx import ASGITransport, AsyncClient

from apps.api.deps import db_session
from apps.api.main import app
from packages.core.worker_controls import (
    ALL_WORKER_KINDS,
    load_worker_controls,
    save_worker_controls,
)


def test_worker_controls_persist_and_filter_active_kinds(tmp_path: Path, monkeypatch) -> None:
    controls_file = tmp_path / "worker-controls.json"
    monkeypatch.setenv("JOBRUNNER_WORKER_CONTROLS_FILE", str(controls_file))

    initial = load_worker_controls()
    assert initial.crawler_paused is False
    assert initial.max_workers == 1
    assert initial.enabled_kinds == list(ALL_WORKER_KINDS)

    paused = save_worker_controls(crawler_paused=True, max_workers=4)
    assert paused.crawler_paused is True
    assert paused.max_workers == 4
    active = paused.active_kinds(ALL_WORKER_KINDS)
    assert "rescore" in active
    assert "apply" in active
    assert "inbox" in active
    assert "crawl" not in active
    assert "crawl_company" not in active
    assert "discover_company" not in active

    subset = save_worker_controls(
        crawler_paused=False,
        enabled_kinds=["rescore", "invalid_kind"],
        max_workers=99,
    )
    assert subset.crawler_paused is False
    assert subset.enabled_kinds == ["rescore"]
    assert subset.max_workers == 8
    assert subset.active_kinds(ALL_WORKER_KINDS) == ["rescore"]


async def test_worker_run_once_respects_enabled_kinds_and_pause(
    tmp_path: Path, monkeypatch
) -> None:
    from apps.worker import run as worker_run

    controls_file = tmp_path / "worker-controls.json"
    monkeypatch.setenv("JOBRUNNER_WORKER_CONTROLS_FILE", str(controls_file))

    save_worker_controls(enabled_kinds=[])
    assert await worker_run.run_once(worker_id="w1") is False

    save_worker_controls(
        crawler_paused=True,
        enabled_kinds=["crawl", "crawl_company", "rescore"],
    )
    captured_kinds: list[str] = []

    async def fake_claim(session, *, worker_id, kinds, after_others, lease_seconds):
        captured_kinds.extend(kinds)
        return None

    fake_session = AsyncMock()
    fake_cm = AsyncMock()
    fake_cm.__aenter__.return_value = fake_session
    fake_cm.__aexit__.return_value = None
    monkeypatch.setattr(worker_run.core_db, "get_sessionmaker", lambda: lambda: fake_cm)
    monkeypatch.setattr(worker_run, "claim_task", fake_claim)

    assert await worker_run.run_once(worker_id="w1") is False
    assert captured_kinds == ["rescore"]


async def test_crawl_pause_resume_stop_and_controls_routes(tmp_path: Path, monkeypatch) -> None:
    from apps.api.routers import crawl as crawl_router
    from packages.core import heartbeat

    controls_file = tmp_path / "worker-controls.json"
    monkeypatch.setenv("JOBRUNNER_WORKER_CONTROLS_FILE", str(controls_file))

    fake_session = AsyncMock()

    async def override_db():
        yield fake_session

    monkeypatch.setattr(heartbeat, "workers", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        crawl_router,
        "request_crawl",
        AsyncMock(return_value=type("R", (), {"queued": object(), "waiting": 0})()),
    )
    monkeypatch.setattr(crawl_router, "cancel_pending", AsyncMock(return_value=[1, 2]))

    app.dependency_overrides[db_session] = override_db
    try:
        transport = ASGITransport(app=app, client=("127.0.0.1", 12345))
        async with AsyncClient(transport=transport, base_url="http://localhost") as client:
            pause_res = await client.post("/crawl/pause")
            assert pause_res.status_code == 200
            assert pause_res.json()["paused"] is True

            get_res = await client.get("/crawl/controls")
            assert get_res.status_code == 200
            assert get_res.json()["crawler_paused"] is True

            put_res = await client.put(
                "/crawl/controls",
                json={"max_workers": 4, "enabled_kinds": ["crawl", "rescore"]},
            )
            assert put_res.status_code == 200
            assert put_res.json()["max_workers"] == 4
            assert put_res.json()["enabled_kinds"] == ["crawl", "rescore"]

            stop_res = await client.post("/crawl/stop")
            assert stop_res.status_code == 200
            assert stop_res.json()["paused"] is True
            assert stop_res.json()["cancelled"] == 4

            resume_res = await client.post("/crawl/resume")
            assert resume_res.status_code == 200
            assert resume_res.json()["queued"] is True
    finally:
        app.dependency_overrides.pop(db_session, None)
