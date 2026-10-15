"""A posting on a board that has stopped answering says so on its card.

A crawl that fails closes nothing, rightly: a board that is down this
afternoon is not a board whose jobs have gone (packages/crawler/CLAUDE.md).
The cost of that rule is a posting that stays open for as long as its board
stays down. On 2026-10-14 the audit found 60 open postings on 8 failing
boards, the oldest last answered on 2026-09-18, and nothing on their cards
said anything was different about them.

`board_unanswered` is the fact (`consecutive_failures` above zero) and
`board_last_answered_at` is when it last did. The card names the date and
says the posting may have closed. It is not a filter and closes nothing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Company, CompanyCrawlState, Match, Posting

LAST_ANSWER = datetime(2026, 9, 18, 3, 36, tzinfo=UTC)


async def _scored(session: AsyncSession, profile_id: uuid.UUID, *, failures: int) -> uuid.UUID:
    company = Company(name=f"Acme {uuid.uuid4().hex[:6]}")
    session.add(company)
    await session.flush()
    session.add(
        CompanyCrawlState(
            company_id=company.id,
            consecutive_failures=failures,
            last_success_at=LAST_ANSWER,
            last_attempt_at=LAST_ANSWER + timedelta(days=26),
            last_status="error" if failures else "ok",
        )
    )
    posting = Posting(
        company_id=company.id,
        url=f"https://boards.greenhouse.io/acme/jobs/{uuid.uuid4().hex[:8]}",
        title="Backend Engineer",
    )
    session.add(posting)
    await session.flush()
    session.add(Match(profile_id=profile_id, posting_id=posting.id, score=0.5, reasons_json={}))
    await session.commit()
    return posting.id


@pytest.mark.parametrize(("failures", "unanswered"), [(3, True), (0, False)])
async def test_a_failing_board_is_named_on_its_postings(
    client: AsyncClient,
    worker_session: AsyncSession,
    complete_candidate,
    failures: int,
    unanswered: bool,
) -> None:
    profile_id = uuid.UUID(complete_candidate["profile_id"])
    posting_id = await _scored(worker_session, profile_id, failures=failures)

    rows = (await client.get("/matches", params={"profile_id": str(profile_id)})).json()

    (row,) = [row for row in rows if row["posting_id"] == str(posting_id)]
    assert row["board_unanswered"] is unanswered
    if unanswered:
        assert datetime.fromisoformat(row["board_last_answered_at"]) == LAST_ANSWER
    else:
        assert row["board_last_answered_at"] is None
    assert row["closed"] is False


async def test_a_posting_with_no_company_is_not_called_unanswered(
    client: AsyncClient, worker_session: AsyncSession, complete_candidate
) -> None:
    profile_id = uuid.UUID(complete_candidate["profile_id"])
    posting = Posting(url="https://boards.greenhouse.io/acme/jobs/loose", title="Loose Posting")
    worker_session.add(posting)
    await worker_session.flush()
    worker_session.add(
        Match(profile_id=profile_id, posting_id=posting.id, score=0.5, reasons_json={})
    )
    await worker_session.commit()

    rows = (await client.get("/matches", params={"profile_id": str(profile_id)})).json()

    (row,) = [row for row in rows if row["posting_id"] == str(posting.id)]
    assert row["board_unanswered"] is False


def test_the_card_says_so() -> None:
    page = (Path(__file__).resolve().parent.parent / "apps/web/src/app/matches/page.tsx").read_text(
        encoding="utf-8"
    )

    assert "match.board_unanswered" in page
    assert "match.board_last_answered_at" in page
