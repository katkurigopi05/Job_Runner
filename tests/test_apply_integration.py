"""End-to-end apply: worker → real pipeline → real adapter → real browser.

This closes a seam. tests/test_worker.py stubs `_run_pipeline`, so nothing
there would notice if `handle_apply` and the pipeline drifted apart — which is
exactly the bug class that bit during Phase 1 (a changed signature the stubbed
tests could not see). Here the only thing faked is the network: Playwright
serves the local Greenhouse fixture in place of the live posting.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from apps.worker import browser as browser_mod
from apps.worker import run as worker_run
from packages.core.config import get_settings
from packages.core.enums import ApplicationStatus, QueueTaskStatus
from packages.core.models import QueueTask
from packages.core.storage import LocalStorage, set_storage

FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse_posting.html"
APPLY_URL = "https://boards.greenhouse.io/acme/jobs/4012345"


@pytest.fixture(autouse=True)
def _serve_fixture(monkeypatch, tmp_path):
    """Intercept navigation and serve the fixture. Everything else is real."""
    body = FIXTURE.read_text()

    @asynccontextmanager
    async def _page(ats: str, **kwargs):
        from apps.worker.browser import ephemeral_page

        async with ephemeral_page() as page:
            await page.route(
                "**/*",
                lambda route: asyncio.ensure_future(
                    route.fulfill(status=200, content_type="text/html", body=body)
                ),
            )
            yield page

    monkeypatch.setattr(browser_mod, "browser_page", _page)
    monkeypatch.setattr("apps.worker.apply_job.browser_page", _page)

    set_storage(LocalStorage(tmp_path / "storage"))
    yield
    set_storage(None)


async def test_full_apply_parks_with_the_exact_questions(
    client: AsyncClient, complete_candidate, tmp_path
) -> None:
    """The real pipeline runs, and parks carrying the employer's wording."""
    created = await client.post("/applications", json={**complete_candidate, "url": APPLY_URL})
    app_id = created.json()["id"]

    assert await worker_run.run_once(worker_id="integration-worker") is True

    polled = (await client.get(f"/applications/{app_id}")).json()
    assert polled["status"] == ApplicationStatus.NEEDS_REVIEW

    review = polled["review"]
    unanswered = {q["question"] for q in review["unanswered"]}
    assert "Why do you want to work at Acme?" in unanswered
    assert "Are you legally authorized to work in the United States?" in unanswered


async def test_full_apply_fills_from_the_profile(client: AsyncClient, complete_candidate) -> None:
    created = await client.post("/applications", json={**complete_candidate, "url": APPLY_URL})
    app_id = created.json()["id"]

    await worker_run.run_once(worker_id="integration-worker")

    review = (await client.get(f"/applications/{app_id}")).json()["review"]
    filled = {f["key"]: f["value"] for f in review["filled"]}

    assert filled["first_name"] == "Test"
    assert filled["email"].startswith("owner-")
    assert filled["phone"] == "+1-555-0100"
    assert review["fill_rate"] > 0


async def test_full_apply_writes_a_screenshot(
    client: AsyncClient, complete_candidate, tmp_path
) -> None:
    """Gate 1: a screenshot lands in storage/receipts/."""
    created = await client.post("/applications", json={**complete_candidate, "url": APPLY_URL})
    app_id = created.json()["id"]

    await worker_run.run_once(worker_id="integration-worker")

    review = (await client.get(f"/applications/{app_id}")).json()["review"]
    ref = review["screenshot_ref"]
    assert ref == f"receipts/{app_id}/filled-form.png"

    stored = tmp_path / "storage" / ref
    assert stored.is_file()
    assert stored.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


async def test_ats_is_recorded_on_the_application(client: AsyncClient, complete_candidate) -> None:
    created = await client.post("/applications", json={**complete_candidate, "url": APPLY_URL})
    await worker_run.run_once(worker_id="integration-worker")

    listing = (await client.get("/applications")).json()
    assert listing[0]["ats"] == "greenhouse"
    assert created.json()["id"] == listing[0]["id"]


async def test_unsupported_site_fails_without_a_browser(
    client: AsyncClient, complete_candidate, worker_session
) -> None:
    """No adapter claims the URL, so it fails rather than parking forever."""
    created = await client.post(
        "/applications", json={**complete_candidate, "url": "https://acme.com/careers/1"}
    )
    app_id = created.json()["id"]

    await worker_run.run_once(worker_id="integration-worker")

    polled = (await client.get(f"/applications/{app_id}")).json()
    assert polled["status"] == ApplicationStatus.FAILED
    assert polled["failure_reason"] == "unsupported_site"


async def test_task_completes_even_when_application_fails(
    client: AsyncClient, complete_candidate, worker_session
) -> None:
    """A handled failure is a finished task, not a retry loop."""
    await client.post(
        "/applications", json={**complete_candidate, "url": "https://acme.com/careers/2"}
    )
    await worker_run.run_once(worker_id="integration-worker")

    task = await worker_session.scalar(select(QueueTask))
    assert task is not None
    await worker_session.refresh(task)
    assert task.status == QueueTaskStatus.DONE


async def test_approval_with_answers_resumes_and_submits(
    client: AsyncClient, auto_submit_candidate, monkeypatch
) -> None:
    """The whole loop: park on real questions, answer them, resume, submit."""
    created = await client.post("/applications", json={**auto_submit_candidate, "url": APPLY_URL})
    app_id = created.json()["id"]

    await worker_run.run_once(worker_id="integration-worker")
    parked = (await client.get(f"/applications/{app_id}")).json()
    assert parked["status"] == ApplicationStatus.NEEDS_REVIEW

    # The résumé is attached to the profile, so only the employer's custom
    # questions are still open.
    open_keys = {q["key"] for q in parked["review"]["unanswered"]}
    assert "resume" not in open_keys, "an attached résumé should fill the file field"

    answers = {
        "job_application_answers_attributes_1_boolean_value": "1",
        "job_application_answers_attributes_2_text_value": "I admire the work.",
    }

    approved = await client.post(
        f"/applications/{app_id}/review", json={"approve": True, "answers": answers}
    )
    assert approved.status_code == 200

    monkeypatch.setenv("AUTO_SUBMIT", "true")
    get_settings.cache_clear()
    try:
        await worker_run.run_once(worker_id="integration-worker")
    finally:
        monkeypatch.delenv("AUTO_SUBMIT", raising=False)
        get_settings.cache_clear()

    final = (await client.get(f"/applications/{app_id}")).json()
    # Everything required is now answered, so the approved run completes.
    assert final["status"] == ApplicationStatus.SUBMITTED, final["review"].get("unanswered")
