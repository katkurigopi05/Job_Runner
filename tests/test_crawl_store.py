"""What `_store` does with a posting it has seen before.

The batch upsert is the performance half of this change and the dull half to
test. The half worth pinning is what a *sighting* now means: a posting the
board still lists is recorded as still listed, and a posting that comes back
is open again whether or not a single character of it changed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select

from packages.core.models import Posting
from packages.crawler.crawl import crawl_company
from packages.crawler.extract import CompanySeed
from packages.crawler.fetch import PoliteFetcher
from packages.crawler.ratelimit import HostRateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def limiter() -> HostRateLimiter:
    clock = FakeClock()
    return HostRateLimiter(clock=clock, sleeper=clock.sleep)


def _job(job_id: int, title: str = "Senior Backend Engineer", content: str = "<p>Python</p>"):
    return {
        "id": job_id,
        "title": title,
        "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        "location": {"name": "Remote"},
        "content": content,
    }


def _fetcher(payload: dict) -> PoliteFetcher:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:")
        return httpx.Response(200, text=json.dumps(payload))

    return PoliteFetcher(transport=httpx.MockTransport(handler), rate_limiter=limiter())


@pytest.fixture
def seed() -> CompanySeed:
    return CompanySeed(name="Acme", slug="acme", poll_interval_s=0)


async def _posting(db_session, external_id: str) -> Posting:
    return await db_session.scalar(select(Posting).where(Posting.external_id == external_id))


async def test_a_relisted_posting_reopens_even_when_nothing_changed(db_session, seed) -> None:
    """The bug this rewrite exists to fix.

    `closed_at = None` used to live inside the branch taken only when the
    content hash differed. A requisition put on hold and then resumed comes
    back byte for byte identical — the ordinary case — so it stayed closed
    forever: invisible to matching, while the board said plainly it was open.
    """
    both = await crawl_company(db_session, seed, _fetcher({"jobs": [_job(1), _job(2)]}), force=True)
    assert both.new_postings == 2

    await crawl_company(db_session, seed, _fetcher({"jobs": [_job(1)]}), force=True)
    assert (await _posting(db_session, "2")).closed_at is not None

    again = await crawl_company(
        db_session, seed, _fetcher({"jobs": [_job(1), _job(2)]}), force=True
    )

    assert (await _posting(db_session, "2")).closed_at is None, "the board lists it; it is open"
    assert again.reopened_postings == 1
    assert again.updated_postings == 0, "nothing about it changed, so nothing to re-embed"
    assert again.new_postings == 0, "and it is not a new posting either"


async def test_an_unchanged_posting_is_still_recorded_as_seen(db_session, seed) -> None:
    """ "Did it change" and "is it still there" are different questions.

    An identical posting used to be skipped outright, so the only evidence it
    was still listed was `_close_missing` not having closed it — an inference
    from one cycle's set difference, which stops being sound the moment a
    board is paginated or a cycle is split across workers.
    """
    payload = {"jobs": [_job(1)]}
    await crawl_company(db_session, seed, _fetcher(payload), force=True)
    first = (await _posting(db_session, "1")).last_seen_at
    assert first is not None

    # A second board whose *response* differs (so the board-level hash does
    # not short-circuit) but whose posting 1 is untouched.
    await crawl_company(db_session, seed, _fetcher({"jobs": [_job(1), _job(9)]}), force=True)

    posting = await _posting(db_session, "1")
    assert posting.last_seen_at > first, "still listed, and now recorded as such"
    assert posting.content_hash is not None


async def test_first_seen_at_survives_being_seen_again(db_session, seed) -> None:
    """A posting we notice again was not born again."""
    await crawl_company(db_session, seed, _fetcher({"jobs": [_job(1)]}), force=True)
    born = (await _posting(db_session, "1")).first_seen_at

    await crawl_company(db_session, seed, _fetcher({"jobs": [_job(1), _job(2)]}), force=True)

    assert (await _posting(db_session, "1")).first_seen_at == born


async def test_a_moved_posting_gets_its_new_url(db_session, seed) -> None:
    """`posting_hash` covers the body, not the link.

    So a posting whose URL changed while its text did not used to keep the old
    link indefinitely — and the apply pipeline would follow a dead one and
    report the posting closed.
    """
    await crawl_company(db_session, seed, _fetcher({"jobs": [_job(1)]}), force=True)

    moved = _job(1)
    moved["absolute_url"] = "https://job-boards.greenhouse.io/acme/jobs/1"
    await crawl_company(db_session, seed, _fetcher({"jobs": [moved, _job(3)]}), force=True)

    assert (await _posting(db_session, "1")).url.endswith("job-boards.greenhouse.io/acme/jobs/1")


async def test_an_edited_posting_reads_as_updated_not_new(db_session, seed) -> None:
    await crawl_company(db_session, seed, _fetcher({"jobs": [_job(1)]}), force=True)

    edited = await crawl_company(
        db_session, seed, _fetcher({"jobs": [_job(1, title="Staff Backend Engineer")]}), force=True
    )

    assert edited.updated_postings == 1
    assert edited.new_postings == 0
    assert (await _posting(db_session, "1")).title == "Staff Backend Engineer"


async def test_the_change_set_names_what_downstream_must_revisit(db_session, seed) -> None:
    """Ids, not counts — matching cannot narrow its work from a number."""
    first = await crawl_company(db_session, seed, _fetcher({"jobs": [_job(1)]}), force=True)
    assert len(first.changed_posting_ids) == 1

    second = await crawl_company(
        db_session,
        seed,
        _fetcher({"jobs": [_job(1), _job(2, title="Data Engineer")]}),
        force=True,
    )

    posting_two = await _posting(db_session, "2")
    assert second.changed_posting_ids == [posting_two.id], "only the new one needs revisiting"


async def test_a_published_date_is_not_erased_by_a_board_that_stops_reporting_it(
    db_session, seed
) -> None:
    """It is the only evidence that `poll_interval_s` is set sensibly."""
    dated = _job(1)
    dated["first_published"] = "2026-01-15T00:00:00Z"
    await crawl_company(db_session, seed, _fetcher({"jobs": [dated]}), force=True)
    published = (await _posting(db_session, "1")).published_at
    assert published is not None

    undated = _job(1, title="Staff Backend Engineer")
    await crawl_company(db_session, seed, _fetcher({"jobs": [undated]}), force=True)

    assert (await _posting(db_session, "1")).published_at == published


async def test_two_companies_may_share_an_external_id(db_session) -> None:
    """The unique constraint is per company, not global.

    Greenhouse numbers jobs per board, so id `1` at one employer and id `1` at
    another are unrelated postings that must not collide.
    """
    acme = CompanySeed(name="Acme", slug="acme", poll_interval_s=0)
    other = CompanySeed(name="Globex", slug="globex", poll_interval_s=0)

    await crawl_company(db_session, acme, _fetcher({"jobs": [_job(1)]}), force=True)
    result = await crawl_company(db_session, other, _fetcher({"jobs": [_job(1)]}), force=True)

    assert result.new_postings == 1
    rows = (await db_session.scalars(select(Posting).where(Posting.external_id == "1"))).all()
    assert len(rows) == 2


async def test_an_empty_board_stores_nothing_and_does_not_raise(db_session, seed) -> None:
    """A bulk INSERT with no VALUES is a syntax error, not an empty write."""
    result = await crawl_company(db_session, seed, _fetcher({"jobs": []}), force=True)

    assert result.new_postings == 0
    assert result.changed_posting_ids == []


async def test_last_seen_at_is_set_when_a_posting_is_first_stored(db_session, seed) -> None:
    before = datetime.now(UTC)
    await crawl_company(db_session, seed, _fetcher({"jobs": [_job(1)]}), force=True)

    posting = await _posting(db_session, "1")
    assert posting.last_seen_at is not None
    assert posting.last_seen_at >= before


async def test_a_board_listing_the_same_posting_twice_does_not_kill_the_cycle(
    db_session, seed
) -> None:
    """Postgres refuses an upsert whose VALUES touch one row twice.

    It refuses the whole statement, not the duplicate — so one malformed board
    would cost every posting in it, and the cycle would report a failed
    company for what is really a cosmetic defect in someone's feed.
    """
    twice = {"jobs": [_job(1), _job(2), _job(1, title="Senior Backend Engineer (duplicate)")]}

    result = await crawl_company(db_session, seed, _fetcher(twice), force=True)

    assert result.error is None
    assert result.new_postings == 2, "the duplicate is one posting, not two"
    assert (await _posting(db_session, "1")).title == "Senior Backend Engineer (duplicate)", (
        "the last listing wins, as a sequential read would have left it"
    )


async def test_closing_is_decided_by_the_sighting_stamp_not_a_list_of_ids(db_session, seed) -> None:
    """The two halves of the write must share one clock reading.

    `_close_missing` closes whatever still carries a stamp older than the one
    `_store` just wrote. If the two read the clock separately, every posting
    on the board looks stale and the whole board closes — so this checks the
    ordinary case really does leave the listed postings alone.
    """
    await crawl_company(db_session, seed, _fetcher({"jobs": [_job(1), _job(2)]}), force=True)

    result = await crawl_company(
        db_session, seed, _fetcher({"jobs": [_job(1), _job(2), _job(3)]}), force=True
    )

    assert result.closed_postings == 0, "everything on the board stays open"
    assert (await _posting(db_session, "1")).closed_at is None
    assert (await _posting(db_session, "2")).closed_at is None

    dropped = await crawl_company(db_session, seed, _fetcher({"jobs": [_job(1)]}), force=True)
    assert dropped.closed_postings == 2, "and the two the board stopped listing do close"
