"""Worker tests — Gate 0.

Covers the remaining gate assertion: POST /applications, run the worker, poll
GET /applications/{id} until it reaches a terminal state.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from apps.worker import apply_job
from apps.worker import run as worker_run
from packages.core.config import get_settings
from packages.core.enums import ApplicationStatus, QueueTaskStatus
from packages.core.models import Application, QueueTask
from packages.core.queue import claim_task, expire_leases

APPLY_URL = "https://boards.greenhouse.io/acme/jobs/98765"


@pytest.fixture(autouse=True)
def _fast_stub(monkeypatch):
    """The Phase 0 stub sleeps 2s; tests do not need to wait for it."""
    monkeypatch.setattr(apply_job, "STUB_WORK_SECONDS", 0.0)


@pytest.fixture
def _auto_submit(monkeypatch):
    """Flip AUTO_SUBMIT on, bypassing the approval gate for one test."""
    monkeypatch.setenv("AUTO_SUBMIT", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("AUTO_SUBMIT", raising=False)
    get_settings.cache_clear()


async def _drain(worker_id: str = "test-worker", limit: int = 10) -> int:
    processed = 0
    for _ in range(limit):
        if not await worker_run.run_once(worker_id=worker_id):
            break
        processed += 1
    return processed


async def test_application_parks_at_needs_review_by_default(
    client: AsyncClient, complete_candidate
) -> None:
    """The shipped default holds for approval instead of submitting."""
    created = await client.post("/applications", json={**complete_candidate, "url": APPLY_URL})
    app_id = created.json()["id"]

    assert await _drain() == 1

    polled = await client.get(f"/applications/{app_id}")
    assert polled.json()["status"] == ApplicationStatus.NEEDS_REVIEW


async def test_application_reaches_submitted_with_auto_submit(
    client: AsyncClient, complete_candidate, _auto_submit
) -> None:
    """Gate 0: POST /applications -> poll GET /applications/{id} -> submitted."""
    created = await client.post("/applications", json={**complete_candidate, "url": APPLY_URL})
    app_id = created.json()["id"]

    assert await _drain() == 1

    polled = await client.get(f"/applications/{app_id}")
    assert polled.json()["status"] == ApplicationStatus.SUBMITTED
    assert polled.json()["failure_reason"] is None


async def test_every_transition_has_an_event(
    client: AsyncClient, complete_candidate, _auto_submit
) -> None:
    """Gate 0: ApplicationEvent rows exist for every transition."""
    created = await client.post("/applications", json={**complete_candidate, "url": APPLY_URL})
    await _drain()

    events = await client.get(f"/applications/{created.json()['id']}/events")
    types = [e["type"] for e in events.json()]

    assert types == ["created", "transition", "transition"]
    edges = [(e["payload"]["from"], e["payload"]["to"]) for e in events.json()[1:]]
    assert edges == [("queued", "running"), ("running", "submitted")]


async def test_approval_resumes_and_submits(
    client: AsyncClient, complete_candidate, monkeypatch
) -> None:
    """The full gate: park, approve, resume, submit."""
    created = await client.post("/applications", json={**complete_candidate, "url": APPLY_URL})
    app_id = created.json()["id"]

    await _drain()
    assert (await client.get(f"/applications/{app_id}")).json()["status"] == "needs_review"

    # Approving enqueues a fresh task; auto-submit lets the resumed run finish.
    approved = await client.post(
        f"/applications/{app_id}/review",
        json={"approve": True, "answers": {"why_us": "I like the product"}},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "running"

    monkeypatch.setenv("AUTO_SUBMIT", "true")
    get_settings.cache_clear()
    try:
        await _drain()
    finally:
        monkeypatch.delenv("AUTO_SUBMIT", raising=False)
        get_settings.cache_clear()

    assert (await client.get(f"/applications/{app_id}")).json()["status"] == "submitted"


async def test_rejection_fails_with_rejected_at_review(
    client: AsyncClient, complete_candidate
) -> None:
    created = await client.post("/applications", json={**complete_candidate, "url": APPLY_URL})
    app_id = created.json()["id"]
    await _drain()

    rejected = await client.post(
        f"/applications/{app_id}/review", json={"approve": False, "note": "not a fit"}
    )

    assert rejected.status_code == 200
    assert rejected.json()["status"] == "failed"
    assert rejected.json()["failure_reason"] == "rejected_at_review"


async def test_worker_marks_task_done(
    client: AsyncClient, complete_candidate, worker_session
) -> None:
    await client.post("/applications", json={**complete_candidate, "url": APPLY_URL})
    await _drain()

    tasks = (await worker_session.scalars(select(QueueTask))).all()
    assert [t.status for t in tasks] == [QueueTaskStatus.DONE]
    assert tasks[0].locked_by is None
    assert tasks[0].lease_expires_at is None


async def test_empty_queue_is_a_no_op(client: AsyncClient) -> None:
    assert await worker_run.run_once(worker_id="test-worker") is False


# --------------------------------------------------------------------------
# Crash recovery — the lease doing its job end to end
# --------------------------------------------------------------------------


async def test_crashed_run_is_resumed_not_deadlocked(
    client: AsyncClient, complete_candidate, worker_session, _auto_submit
) -> None:
    """Simulate a worker dying after committing queued->running."""
    created = await client.post("/applications", json={**complete_candidate, "url": APPLY_URL})
    app_id = created.json()["id"]

    # Worker A claims and advances the application, then "dies" before acking.
    claimed = await claim_task(worker_session, worker_id="worker-a")
    assert claimed is not None
    application = await worker_session.get(Application, app_id)
    assert application is not None
    from packages.core.state import begin_work

    await begin_work(worker_session, application)
    await worker_session.commit()

    assert (await client.get(f"/applications/{app_id}")).json()["status"] == "running"

    # Its lease goes stale, and the real worker loop picks the task back up.
    await expire_leases(worker_session)
    await worker_session.commit()

    assert await worker_run.run_once(worker_id="worker-a") is True

    polled = await client.get(f"/applications/{app_id}")
    assert polled.json()["status"] == ApplicationStatus.SUBMITTED


async def test_redelivery_after_submission_does_not_resubmit(
    client: AsyncClient, complete_candidate, worker_session, _auto_submit
) -> None:
    """At-least-once delivery must not produce a second submission."""
    created = await client.post("/applications", json={**complete_candidate, "url": APPLY_URL})
    app_id = created.json()["id"]
    await _drain()
    assert (await client.get(f"/applications/{app_id}")).json()["status"] == "submitted"

    before = (await client.get(f"/applications/{app_id}/events")).json()

    # Force the finished task back into the queue and run it again.
    task = await worker_session.scalar(select(QueueTask))
    assert task is not None
    task.status = QueueTaskStatus.PENDING.value
    task.locked_by = None
    task.lease_expires_at = None
    await worker_session.commit()

    assert await worker_run.run_once(worker_id="worker-a") is True

    after = (await client.get(f"/applications/{app_id}/events")).json()
    assert len(after) == len(before)
    assert (await client.get(f"/applications/{app_id}")).json()["status"] == "submitted"


async def test_unknown_application_id_does_not_retry(client: AsyncClient, worker_session) -> None:
    """A payload that can never work must not burn three attempts."""
    import uuid as _uuid

    from packages.core.queue import enqueue

    await enqueue(worker_session, "apply", {"application_id": str(_uuid.uuid4())})
    await worker_session.commit()

    assert await worker_run.run_once(worker_id="worker-a") is True

    task = await worker_session.scalar(select(QueueTask))
    assert task is not None
    await worker_session.refresh(task)
    assert task.status == QueueTaskStatus.FAILED


async def test_handler_error_retries_then_fails_the_application(
    client: AsyncClient, complete_candidate, worker_session, monkeypatch
) -> None:
    """Out of retries, the application must not sit in `running` forever."""
    created = await client.post("/applications", json={**complete_candidate, "url": APPLY_URL})
    app_id = created.json()["id"]

    async def _boom(session, application):
        raise RuntimeError("site exploded")

    monkeypatch.setattr(apply_job, "_run_pipeline", _boom)

    for _ in range(4):
        await worker_run.run_once(worker_id="worker-a")
        task = await worker_session.scalar(select(QueueTask))
        if task is not None:
            await worker_session.refresh(task)
            if task.status == QueueTaskStatus.FAILED:
                break
        # Retries are scheduled into the future; pull them forward.
        if task is not None and task.status == QueueTaskStatus.PENDING:
            from datetime import UTC, datetime

            task.run_after = datetime.now(UTC)
            await worker_session.commit()

    polled = await client.get(f"/applications/{app_id}")
    assert polled.json()["status"] == ApplicationStatus.FAILED
    assert polled.json()["failure_reason"] == "site_error"


async def test_configured_worker_id_is_used(monkeypatch) -> None:
    """WORKER_ID must reach the loop — the hostname is only a fallback."""
    monkeypatch.setenv("WORKER_ID", "configured-worker")
    get_settings.cache_clear()
    seen: list[str] = []

    async def _capture(*, worker_id: str, lease_seconds: int) -> bool:
        seen.append(worker_id)
        return False

    monkeypatch.setattr(worker_run, "run_once", _capture)
    monkeypatch.setattr(worker_run, "IDLE_SLEEP_SECONDS", 0.01)

    task = asyncio.create_task(worker_run.run_forever())
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    get_settings.cache_clear()
    assert seen and seen[0] == "configured-worker"
