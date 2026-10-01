"""Keeping only last month's postings, without losing anything of the owner's.

Two halves, and either alone fails. Deleting old postings without stopping
the crawler storing them is undone by the next crawl; stopping the crawler
the obvious way, by dropping old postings from what it records, closes
postings that are still listed. And a delete that ignores what points at a
posting takes the owner's applications, swipes and grades with it.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from packages.core.config import get_settings
from packages.core.models import (
    Application,
    Candidate,
    Company,
    Match,
    Posting,
    PostingLabel,
    Profile,
    Resume,
    User,
)
from packages.crawler.crawl import crawl_company
from packages.crawler.extract import CompanySeed
from packages.crawler.fetch import PoliteFetcher
from packages.crawler.ratelimit import HostRateLimiter
from packages.crawler.retention import prune

NOW = datetime.now(UTC)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _fetcher(jobs: list[dict]) -> PoliteFetcher:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:")
        return httpx.Response(200, text=json.dumps({"jobs": jobs}))

    clock = _Clock()
    return PoliteFetcher(
        transport=httpx.MockTransport(handler),
        rate_limiter=HostRateLimiter(clock=clock, sleeper=clock.sleep),
    )


def _job(job_id: int, *, days_ago: int | None) -> dict:
    job = {
        "id": job_id,
        "title": f"Engineer {job_id}",
        "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        "location": {"name": "Remote"},
        "content": "<p>Python</p>",
    }
    if days_ago is not None:
        job["first_published"] = (NOW - timedelta(days=days_ago)).isoformat()
    return job


@pytest.fixture
def thirty_days(monkeypatch):
    """The shipped limit, for this test only. The suite stores any age."""
    monkeypatch.setenv("POSTING_MAX_AGE_DAYS", "30")
    get_settings.cache_clear()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


SEED = CompanySeed(name="Acme", slug="acme", poll_interval_s=0)


async def _external_ids(session) -> dict[str, datetime | None]:
    rows = await session.execute(select(Posting.external_id, Posting.closed_at))
    return dict(rows.all())


# --- the crawler stops storing them ---------------------------------------------


async def test_an_old_posting_new_to_the_database_is_not_stored(db_session, thirty_days) -> None:
    result = await crawl_company(
        db_session, SEED, _fetcher([_job(1, days_ago=60), _job(2, days_ago=2)]), force=True
    )

    assert set(await _external_ids(db_session)) == {"2"}
    assert result.new_postings == 1


async def test_an_old_posting_already_held_is_kept_open(db_session, monkeypatch) -> None:
    """The obvious filter would close it.

    `_close_missing` closes whatever a crawl did not stamp as seen. Dropping
    old postings before the store would leave this one unstamped, and a
    posting the board still lists would be marked closed.
    """
    await crawl_company(db_session, SEED, _fetcher([_job(1, days_ago=60)]), force=True)

    monkeypatch.setenv("POSTING_MAX_AGE_DAYS", "30")
    get_settings.cache_clear()
    try:
        # A second posting, so the board has changed: an identical board is
        # skipped before `_store` runs, and this test would prove nothing.
        await crawl_company(
            db_session, SEED, _fetcher([_job(1, days_ago=60), _job(2, days_ago=1)]), force=True
        )
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()

    assert await _external_ids(db_session) == {"1": None, "2": None}, "still listed, still open"


async def test_a_posting_with_no_date_is_stored(db_session, thirty_days) -> None:
    """An unknown age is not evidence of an old one."""
    await crawl_company(db_session, SEED, _fetcher([_job(1, days_ago=None)]), force=True)

    assert set(await _external_ids(db_session)) == {"1"}


# --- prune deletes the rest, never the owner's ------------------------------------


async def _owner(session) -> tuple[Candidate, Profile]:
    user = User(email=f"o-{uuid.uuid4().hex[:8]}@example.com")
    session.add(user)
    await session.flush()
    candidate = Candidate(user_id=user.id, name="Owner", email="c@example.com")
    session.add(candidate)
    await session.flush()
    profile = Profile(candidate_id=candidate.id, label="p")
    session.add(profile)
    await session.flush()
    return candidate, profile


async def _posting(session, company: Company, name: str, *, days_ago: int | None, closed=False):
    posting = Posting(
        company_id=company.id,
        external_id=name,
        url=f"https://x.test/{name}",
        title=name,
        published_at=None if days_ago is None else NOW - timedelta(days=days_ago),
        closed_at=NOW if closed else None,
    )
    session.add(posting)
    await session.flush()
    return posting


async def _world(session) -> dict[str, Posting]:
    company = Company(name="Acme", ats_type="greenhouse")
    session.add(company)
    await session.flush()
    candidate, profile = await _owner(session)
    p = {
        "fresh": await _posting(session, company, "fresh", days_ago=3),
        "old": await _posting(session, company, "old", days_ago=60),
        "closed": await _posting(session, company, "closed", days_ago=3, closed=True),
        "undated": await _posting(session, company, "undated", days_ago=None),
        "undated_closed": await _posting(
            session, company, "undated_closed", days_ago=None, closed=True
        ),
        "old_swiped_no": await _posting(session, company, "old_swiped_no", days_ago=60),
        "old_scored": await _posting(session, company, "old_scored", days_ago=60),
        "old_applied": await _posting(session, company, "old_applied", days_ago=60),
        "old_tailored": await _posting(session, company, "old_tailored", days_ago=60),
        "old_graded": await _posting(session, company, "old_graded", days_ago=60),
    }
    session.add_all(
        [
            Match(
                profile_id=profile.id, posting_id=p["old_swiped_no"].id, score=0.1, decision="no"
            ),
            Match(profile_id=profile.id, posting_id=p["old_scored"].id, score=0.1),
            Application(
                candidate_id=candidate.id,
                profile_id=profile.id,
                posting_id=p["old_applied"].id,
                url=p["old_applied"].url,
            ),
            Resume(
                candidate_id=candidate.id,
                storage_ref="r.pdf",
                tailored_for_posting_id=p["old_tailored"].id,
            ),
            PostingLabel(profile_id=profile.id, posting_id=p["old_graded"].id, relevance=2),
        ]
    )
    await session.flush()
    return p


async def _names(session) -> set[str]:
    return set((await session.scalars(select(Posting.external_id))).all())


async def test_a_dry_run_deletes_nothing(db_session) -> None:
    await _world(db_session)

    report = await prune(db_session, max_age_days=30, now=NOW, apply=False)

    assert len(await _names(db_session)) == 10
    assert (report.stale, report.kept_for_owner, report.deleted) == (8, 4, 0)
    assert report.remaining == 6


async def test_old_and_closed_postings_go_and_the_owners_stay(db_session) -> None:
    """Applied to, tailored for, graded, swiped on: each kept, though old.

    A score alone is not the owner's: a match with no decision is regenerated
    by the next matching pass, so its posting goes, and the match with it.
    """
    await _world(db_session)

    report = await prune(db_session, max_age_days=30, now=NOW, apply=True)

    assert await _names(db_session) == {
        "fresh",
        "undated",
        "old_swiped_no",
        "old_applied",
        "old_tailored",
        "old_graded",
    }
    assert report.deleted == 4
    assert await db_session.scalar(select(func.count()).select_from(Match)) == 1


async def test_a_tailored_resume_never_loses_its_posting(db_session) -> None:
    """`ON DELETE SET NULL` would make it read as a base résumé."""
    await _world(db_session)

    await prune(db_session, max_age_days=30, now=NOW, apply=True)

    tailored = await db_session.scalar(select(Resume))
    assert tailored.tailored_for_posting_id is not None


async def test_no_age_limit_still_deletes_closed_postings(db_session) -> None:
    await _world(db_session)

    await prune(db_session, max_age_days=0, now=NOW, apply=True)

    names = await _names(db_session)
    assert "closed" not in names and "undated_closed" not in names
    assert "old" in names, "age is not a reason when the limit is off"
