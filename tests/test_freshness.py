"""Publication date and crawler lag.

`first_seen_at` records when the crawler noticed a posting. Nothing recorded
when it was *published*, so "are we late?" was unanswerable — and lag is the
only evidence that `poll_interval_s` is set sensibly. A competitor sells a
60-minute SLA on exactly this number (`docs/REFERENCE.md`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Match, Posting
from packages.crawler.extract import (
    GreenhouseExtractor,
    LeverExtractor,
    parse_timestamp,
)


def test_greenhouse_iso_with_offset() -> None:
    parsed = parse_timestamp("2026-08-06T12:50:10-04:00")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.astimezone(UTC).hour == 16


def test_lever_epoch_milliseconds() -> None:
    """Lever sends milliseconds; seconds would put it in 1970."""
    parsed = parse_timestamp(1750119882479)
    assert parsed is not None
    assert parsed.year == 2025


def test_seconds_are_not_mistaken_for_milliseconds() -> None:
    parsed = parse_timestamp(1750119882)
    assert parsed is not None
    assert parsed.year == 2025


@pytest.mark.parametrize("value", [None, "", "not a date", {}, []])
def test_unparseable_returns_none_rather_than_a_guess(value) -> None:
    """A wrong publication date makes the lag lie. A missing one is honest."""
    assert parse_timestamp(value) is None


def test_greenhouse_prefers_first_published_over_updated_at() -> None:
    """updated_at moves on any later edit; first_published is when it went up."""
    body = """{"jobs": [{"id": 1, "title": "Engineer", "absolute_url": "https://x/1",
               "first_published": "2026-08-01T10:00:00-04:00",
               "updated_at": "2026-08-13T17:38:18-04:00"}]}"""

    posting = GreenhouseExtractor().parse(body, "acme")[0]

    assert posting.published_at is not None
    assert posting.published_at.astimezone(UTC).day == 1


def test_lever_reads_created_at() -> None:
    body = """[{"id": "abc", "text": "Engineer", "hostedUrl": "https://jobs.lever.co/x/abc",
               "createdAt": 1750119882479, "description": "Work."}]"""

    posting = LeverExtractor().parse(body, "acme")[0]

    assert posting.published_at is not None
    assert posting.published_at.year == 2025


def test_a_board_without_dates_still_parses() -> None:
    """No date is a fact about the board, not a parse failure."""
    body = """{"jobs": [{"id": 2, "title": "Engineer", "absolute_url": "https://x/2"}]}"""

    posting = GreenhouseExtractor().parse(body, "acme")[0]

    assert posting.published_at is None
    assert posting.title == "Engineer"


# --------------------------------------------------------------------------
# Lag, over the API
# --------------------------------------------------------------------------


@pytest.fixture
async def scored_with_dates(
    client: AsyncClient, worker_session: AsyncSession, complete_candidate
) -> str:
    profile_id = uuid.UUID(complete_candidate["profile_id"])
    now = datetime.now(UTC)

    dated = Posting(
        url="https://boards.greenhouse.io/acme/jobs/dated",
        title="Dated Posting",
        published_at=now - timedelta(hours=5),
        first_seen_at=now - timedelta(hours=2),
    )
    undated = Posting(url="https://boards.greenhouse.io/acme/jobs/undated", title="Undated Posting")
    worker_session.add_all([dated, undated])
    await worker_session.flush()
    worker_session.add_all(
        [
            Match(profile_id=profile_id, posting_id=dated.id, score=0.9, reasons_json={}),
            Match(profile_id=profile_id, posting_id=undated.id, score=0.8, reasons_json={}),
        ]
    )
    await worker_session.commit()
    return str(profile_id)


async def test_lag_is_reported_in_hours(client: AsyncClient, scored_with_dates) -> None:
    rows = (await client.get("/matches", params={"profile_id": scored_with_dates})).json()
    dated = next(r for r in rows if r["title"] == "Dated Posting")

    assert dated["lag_hours"] == pytest.approx(3.0, abs=0.05)


async def test_an_unmeasurable_lag_is_null_not_zero(client: AsyncClient, scored_with_dates) -> None:
    """Zero would flatter the number the measurement exists to question."""
    rows = (await client.get("/matches", params={"profile_id": scored_with_dates})).json()
    undated = next(r for r in rows if r["title"] == "Undated Posting")

    assert undated["published_at"] is None
    assert undated["lag_hours"] is None
