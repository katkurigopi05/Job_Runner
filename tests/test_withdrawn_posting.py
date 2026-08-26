"""A job taken down is `job_closed`, not `site_error`.

The adapters read "closed" from on-page text — "this posting is closed",
"position has been filled". A posting deleted outright carries none of that: the
board returns 404, `parse_posting` sees nothing that looks closed, and
`enumerate_fields` then finds no form and raises `SiteError`.

So the most ordinary outcome in a job search — the role is gone — was recorded
under a code that means *our side is broken*, and invites a retry that can only
fail the same way. Found on a real Lever posting whose URL now 404s, filed as
`site_error` alongside three genuine captcha blocks.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from httpx import AsyncClient

from apps.worker import browser as browser_mod
from apps.worker import run as worker_run
from packages.core.enums import ApplicationStatus, FailureReason
from packages.core.storage import LocalStorage, set_storage

GONE_URL = "https://boards.greenhouse.io/acme/jobs/9999999"


@pytest.fixture
def _serves(monkeypatch, tmp_path):
    """Serve one status code for every request the pipeline makes."""

    def _install(status: int, body: str = "<html><body>Not Found</body></html>") -> None:
        @asynccontextmanager
        async def _page(ats: str, **kwargs):
            from apps.worker.browser import ephemeral_page

            async with ephemeral_page() as page:
                await page.route(
                    "**/*",
                    lambda route: asyncio.ensure_future(
                        route.fulfill(status=status, content_type="text/html", body=body)
                    ),
                )
                yield page

        monkeypatch.setattr(browser_mod, "browser_page", _page)
        monkeypatch.setattr("apps.worker.apply_job.browser_page", _page)

    set_storage(LocalStorage(tmp_path / "storage"))
    yield _install
    set_storage(None)


@pytest.mark.parametrize("status", [404, 410])
async def test_a_withdrawn_posting_fails_as_job_closed(
    client: AsyncClient, complete_candidate, _serves, status: int
) -> None:
    """404 and 410 both mean the job is gone, and §6 has a code for that."""
    _serves(status)
    created = await client.post("/applications", json={**complete_candidate, "url": GONE_URL})
    app_id = created.json()["id"]

    await worker_run.run_once(worker_id="withdrawn-worker")

    polled = (await client.get(f"/applications/{app_id}")).json()
    assert polled["status"] == ApplicationStatus.FAILED
    assert polled["failure_reason"] == FailureReason.JOB_CLOSED, (
        "a taken-down posting was recorded as something other than job_closed, "
        "which invites a retry that can only fail the same way"
    )


async def test_the_reason_says_what_the_site_returned(
    client: AsyncClient, complete_candidate, _serves
) -> None:
    """The status is worth keeping — "closed" and "404" are different evidence."""
    _serves(404)
    created = await client.post("/applications", json={**complete_candidate, "url": GONE_URL})
    app_id = created.json()["id"]

    await worker_run.run_once(worker_id="withdrawn-worker")

    events = (await client.get(f"/applications/{app_id}/events")).json()
    messages = [str((e.get("payload") or {}).get("message", "")) for e in events]
    assert any("404" in m for m in messages)


async def test_a_block_is_not_treated_as_a_closed_job(
    client: AsyncClient, complete_candidate, _serves
) -> None:
    """403 is usually automation being refused, which §2.5 handles differently.

    Folding it into `job_closed` would tell the owner the role was gone when it
    is still open and simply needs finishing by hand.
    """
    _serves(403, body="<html><body>Forbidden</body></html>")
    created = await client.post("/applications", json={**complete_candidate, "url": GONE_URL})
    app_id = created.json()["id"]

    await worker_run.run_once(worker_id="withdrawn-worker")

    polled = (await client.get(f"/applications/{app_id}")).json()
    assert polled["failure_reason"] != FailureReason.JOB_CLOSED
