"""One task per company, instead of one loop over all of them.

A cycle used to be a `for` loop inside a single queue task: one lease held for
the whole run, everything after the failure point lost if it died, and no way
to use a second worker because a loop is a loop. Dispatching keeps the work
identical — the same `crawl_company` — and makes it divisible.

The part that needs testing is not the crawling, which is covered elsewhere.
It is the bookkeeping that replaces the loop: a run nobody owns has to close
itself, and counters written by several workers at once have to add up.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from apps.worker.crawl_company_job import MalformedCrawlTask, handle_crawl_company
from packages.core.models import Company, CrawlRun, Posting, QueueTask
from packages.crawler.crawl import CompanyResult
from packages.crawler.dispatch import (
    CRAWL_COMPANY_TASK_KIND,
    close_if_complete,
    dispatch,
    due_for_dispatch,
    fold_into_run,
    seed_from,
)
from packages.crawler.runs import start_run


def _company(**kwargs) -> Company:
    defaults = {
        "name": "Acme",
        "slug": "acme",
        "ats_type": "greenhouse",
        "poll_interval_s": 3600,
    }
    return Company(**{**defaults, **kwargs})


# --------------------------------------------------------------------------
# Rebuilding the crawl input from the row
# --------------------------------------------------------------------------


def test_a_row_with_a_slug_names_its_own_board() -> None:
    """What `Company.slug` was added for.

    A dispatched task carries an id. The handler that picks it up cannot read
    the seed file — it may have been edited since, and at 3,500 entries
    re-parsing it per company would cost more than the fetch.
    """
    seed = seed_from(_company())

    assert seed is not None
    assert (seed.name, seed.slug, seed.ats) == ("Acme", "acme", "greenhouse")


@pytest.mark.parametrize(
    "broken",
    [
        pytest.param({"slug": None}, id="no slug"),
        pytest.param({"ats_type": None}, id="no ats"),
        pytest.param({"ats_type": "taleo"}, id="no extractor for that ats"),
    ],
)
def test_a_row_that_cannot_name_a_board_is_refused(broken) -> None:
    """Refused here rather than enqueued and failed later.

    Each of these is a registry problem. Dispatching a task certain to fail
    would turn it into queue noise — retried, backed off, retried again —
    instead of a fact someone can read.
    """
    assert seed_from(_company(**broken)) is None


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


async def test_dispatch_enqueues_one_task_per_company(db_session) -> None:
    companies = [_company(name=f"Company {n}", slug=f"co{n}") for n in range(3)]
    for company in companies:
        db_session.add(company)
    await db_session.flush()
    run = await start_run(db_session, trigger="manual")

    report = await dispatch(db_session, companies, run)

    tasks = (
        await db_session.scalars(select(QueueTask).where(QueueTask.kind == CRAWL_COMPANY_TASK_KIND))
    ).all()
    assert report.enqueued == 3
    assert len(tasks) == 3
    assert {task.payload_json["run_id"] for task in tasks} == {str(run.id)}


async def test_the_run_counts_what_was_dispatched_not_what_was_offered(db_session) -> None:
    """Otherwise a run can never reach its own total and never closes.

    A company the dispatcher declines is one that will never report, so
    counting it would leave the run `running` for ever — which reads exactly
    like a cycle still in progress.
    """
    good = _company(name="Good", slug="good")
    bad = _company(name="Bad", slug=None)
    db_session.add_all([good, bad])
    await db_session.flush()
    run = await start_run(db_session, trigger="manual")

    report = await dispatch(db_session, [good, bad], run)

    assert report.enqueued == 1
    assert report.unusable == ["Bad"]
    assert run.companies_total == 1


# --------------------------------------------------------------------------
# Closing a run nobody owns
# --------------------------------------------------------------------------


async def test_the_last_company_to_report_closes_the_run(db_session) -> None:
    run = await start_run(db_session, trigger="manual", companies_total=2)

    await fold_into_run(db_session, run.id, CompanyResult(company="A", fetched=True))
    assert await close_if_complete(db_session, run.id) is False, "one still outstanding"

    await fold_into_run(db_session, run.id, CompanyResult(company="B", fetched=True))
    assert await close_if_complete(db_session, run.id) is True

    await db_session.refresh(run)
    assert run.status == "completed"
    assert run.finished_at is not None


async def test_a_run_is_closed_exactly_once(db_session) -> None:
    """At-least-once delivery means two tasks can both see a complete count.

    The `status = 'running'` in the WHERE clause is what decides between them:
    both try, and exactly one UPDATE matches a row.
    """
    run = await start_run(db_session, trigger="manual", companies_total=1)
    await fold_into_run(db_session, run.id, CompanyResult(company="A", fetched=True))

    first = await close_if_complete(db_session, run.id)
    second = await close_if_complete(db_session, run.id)

    assert (first, second) == (True, False)


async def test_counters_add_up_across_companies(db_session) -> None:
    """Written as SQL arithmetic, because several workers write this one row.

    `run.postings_new += n` in Python is two workers reading the same number
    and both writing it back, which loses one of them.
    """
    run = await start_run(db_session, trigger="manual", companies_total=3)

    await fold_into_run(
        db_session, run.id, CompanyResult(company="A", fetched=True, new_postings=2)
    )
    await fold_into_run(
        db_session,
        run.id,
        CompanyResult(company="B", fetched=True, updated_postings=1, closed_postings=4),
    )
    await fold_into_run(db_session, run.id, CompanyResult(company="C", error="HTTP 500"))

    await db_session.refresh(run)
    assert run.postings_new == 2
    assert run.postings_updated == 1
    assert run.postings_closed == 4
    assert run.companies_fetched == 2
    assert run.companies_failed == 1


async def test_a_suspect_company_is_named_in_the_run(db_session) -> None:
    run = await start_run(db_session, trigger="manual", companies_total=2)

    await fold_into_run(
        db_session, run.id, CompanyResult(company="Acme", fetched=True, suspect_parse=True)
    )
    await fold_into_run(
        db_session, run.id, CompanyResult(company="Globex", fetched=True, suspect_parse=True)
    )

    await db_session.refresh(run)
    assert sorted(run.suspect_companies) == ["Acme", "Globex"]


async def test_a_blocked_company_still_completes_the_run(db_session) -> None:
    """Being told no is an outcome, not a company that never reported."""
    run = await start_run(db_session, trigger="manual", companies_total=1)

    await fold_into_run(
        db_session,
        run.id,
        CompanyResult(company="A", skipped_reason="robots.txt: Disallow: /", blocked=True),
    )

    assert await close_if_complete(db_session, run.id) is True


# --------------------------------------------------------------------------
# The handler
# --------------------------------------------------------------------------


class _Claimed:
    def __init__(self, payload: dict) -> None:
        self.task = QueueTask(kind=CRAWL_COMPANY_TASK_KIND, payload_json=payload)


def _transport(payload: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:")
        return httpx.Response(200, text=json.dumps(payload))

    return httpx.MockTransport(handler)


async def test_a_company_deleted_between_dispatch_and_claim_is_not_an_error(
    db_session,
) -> None:
    """There is nothing to crawl and never will be — retrying cannot help."""
    await handle_crawl_company(db_session, _Claimed({"company_id": str(uuid.uuid4())}))


@pytest.mark.parametrize("payload", [{}, {"company_id": ""}, {"company_id": "not-a-uuid"}])
async def test_a_payload_that_names_no_company_fails_loudly(db_session, payload) -> None:
    with pytest.raises(MalformedCrawlTask):
        await handle_crawl_company(db_session, _Claimed(payload))


async def test_the_handler_crawls_and_records_against_the_run(db_session, monkeypatch) -> None:
    """End to end through the queue payload, with only the socket faked."""
    company = _company()
    db_session.add(company)
    await db_session.flush()
    run = await start_run(db_session, trigger="manual", companies_total=1)

    jobs = {
        "jobs": [
            {
                "id": 1,
                "title": "Senior Backend Engineer",
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                "location": {"name": "Remote"},
                "content": "<p>Python</p>",
            }
        ]
    }

    from packages.crawler import fetch as fetch_module

    real_build = fetch_module.build_fetcher

    def offline(*args, **kwargs):
        kwargs["transport"] = _transport(jobs)
        # The shared limiter reaches a real database; the point here is the
        # handler's bookkeeping, and §2.6 is measured in its own file.
        kwargs["shared"] = False
        return real_build(*args, **kwargs)

    monkeypatch.setattr("apps.worker.crawl_company_job.build_fetcher", offline)

    await handle_crawl_company(
        db_session,
        _Claimed({"company_id": str(company.id), "run_id": str(run.id), "force": True}),
    )

    postings = (await db_session.scalars(select(Posting))).all()
    assert len(postings) == 1

    stored = await db_session.get(CrawlRun, run.id)
    assert stored.postings_new == 1
    assert stored.companies_fetched == 1
    assert stored.status == "completed", "the only company reported, so the run is done"


# --------------------------------------------------------------------------
# Selecting what to dispatch
# --------------------------------------------------------------------------


async def test_dispatch_selection_is_capped(db_session) -> None:
    """A tick that enqueued the whole registry is one long cycle in disguise."""
    for n in range(5):
        db_session.add(_company(name=f"Company {n}", slug=f"co{n}"))
    await db_session.flush()

    assert len(await due_for_dispatch(db_session, limit=2)) == 2
    assert len(await due_for_dispatch(db_session, limit=2, force=True)) == 2
