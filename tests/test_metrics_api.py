"""Tests for loopback-only Prometheus / OpenMetrics telemetry (`GET /metrics`)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from httpx import ASGITransport, AsyncClient

from apps.api.main import app
from packages.core.metrics import (
    PROMETHEUS_CONTENT_TYPE,
    get_metrics_registry,
    normalize_route_path,
)
from packages.core.queue import ClaimedTask, TaskDeferred


def test_normalize_route_path_collapses_uuids_and_integers() -> None:
    uid = str(uuid.uuid4())
    assert normalize_route_path(f"/postings/{uid}") == "/postings/{id}"
    assert normalize_route_path(f"/profiles/{uid}/evidence") == "/profiles/{id}/evidence"
    assert normalize_route_path("/applications/42/review") == "/applications/{id}/review"
    assert normalize_route_path("/health") == "/health"


async def test_metrics_endpoint_is_loopback_only() -> None:
    remote = ASGITransport(app=app, client=("10.0.0.5", 12345))
    async with AsyncClient(transport=remote, base_url="http://localhost") as client:
        refused = await client.get("/metrics")
    assert refused.status_code == 401
    assert refused.json()["error"]["code"] == "unauthorized"


async def test_metrics_endpoint_tracks_http_requests_and_renders_prometheus() -> None:
    registry = get_metrics_registry()
    registry.reset()

    local = ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=local, base_url="http://localhost") as client:
        root_res = await client.get("/")
        assert root_res.status_code == 200
        assert root_res.json()["metrics"] == "/metrics"

        missing_uid = uuid.uuid4()
        not_found = await client.get(f"/no-such-item/{missing_uid}")
        assert not_found.status_code == 404

        metrics_res = await client.get("/metrics")

    assert metrics_res.status_code == 200
    assert metrics_res.headers["content-type"] == PROMETHEUS_CONTENT_TYPE
    body = metrics_res.text
    assert 'jobrunner_http_requests_total{method="GET",route="/",status="200"} 1' in body
    assert (
        'jobrunner_http_requests_total{method="GET",route="/no-such-item/{id}",status="404"} 1'
        in body
    )
    assert 'jobrunner_http_request_duration_seconds_count{method="GET",route="/"} 1' in body


def test_registry_records_worker_crawler_and_guard_metrics() -> None:
    registry = get_metrics_registry()
    registry.reset()

    registry.record_worker_task(kind="crawl", outcome="done", duration_s=0.42)
    registry.record_worker_task(kind="apply", outcome="failed", duration_s=2.1)
    registry.record_crawler_response(vendor="greenhouse", status_code=200)
    registry.record_crawler_response(vendor="workday", status_code=429)
    registry.record_guard_verification(passed=True)
    registry.record_guard_verification(passed=True)
    registry.record_guard_verification(passed=False)

    snap = registry.snapshot()
    assert snap["worker_tasks_total"] == {"apply:failed": 1, "crawl:done": 1}
    assert snap["crawler_http_responses_total"] == {"greenhouse:200": 1, "workday:429": 1}
    assert snap["guard_verifications_total"] == {"passed": 2, "refused": 1}

    text = registry.render_prometheus()
    assert 'jobrunner_worker_tasks_total{kind="crawl",outcome="done"} 1' in text
    assert 'jobrunner_worker_tasks_total{kind="apply",outcome="failed"} 1' in text
    assert 'jobrunner_worker_task_duration_seconds_count{kind="crawl"} 1' in text
    assert 'jobrunner_crawler_http_responses_total{vendor="greenhouse",status="200"} 1' in text
    assert 'jobrunner_crawler_http_responses_total{vendor="workday",status="429"} 1' in text
    assert 'jobrunner_guard_verifications_total{outcome="passed"} 2' in text
    assert 'jobrunner_guard_verifications_total{outcome="refused"} 1' in text


async def test_worker_process_records_task_outcomes(monkeypatch) -> None:
    from apps.worker import run as worker_run

    registry = get_metrics_registry()
    registry.reset()

    session = AsyncMock()
    task_id = uuid.uuid4()
    claimed_done = ClaimedTask(
        task=SimpleNamespace(id=task_id, kind="rescore", payload_json={}),  # type: ignore[arg-type]
        reclaimed=False,
        previous_owner=None,
        task_id=task_id,
        worker_id="w1",
        attempt=1,
    )

    monkeypatch.setitem(worker_run.HANDLERS, "rescore", AsyncMock(return_value=None))
    monkeypatch.setattr(worker_run, "complete_task", AsyncMock(return_value=True))
    await worker_run._process(session, claimed_done)

    defer_until = datetime.now(UTC) + timedelta(seconds=30)
    monkeypatch.setitem(
        worker_run.HANDLERS,
        "rescore",
        AsyncMock(side_effect=TaskDeferred("host_busy", run_after=defer_until)),
    )
    await worker_run._process(session, claimed_done)

    snap = registry.snapshot()
    assert snap["worker_tasks_total"]["rescore:done"] == 1
    assert snap["worker_tasks_total"]["rescore:deferred"] == 1
