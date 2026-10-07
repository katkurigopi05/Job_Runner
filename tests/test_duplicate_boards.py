"""One board is polled under one company row.

Found on 2026-10-06, when the owner asked for no duplicates. Eleven boards
were each verified under two company rows: Vercel and ZEIT, Elastic and
Elastic.co, Flexport and Deliverr, Teleport and Gravitational. The owner's
sheet lists a company under its old name and its new one, discovery resolved
both to the same board, and nothing asked whether somebody already held it.
So each of those boards was fetched twice a cycle and every posting on it was
stored twice: 282 of 11,429 open postings, same URL and same text. The
assistant had answered a Kafka question with a Vercel job listed as "ZEIT".

`uq_postings_company_external_id` could not see it. The two copies belong to
two companies, and the constraint is per company.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from packages.core.enums import SourceStatus
from packages.core.models import Application, Candidate, Company, Match, Posting, Profile, User
from packages.crawler import duplicate_boards as boards

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
VERIFIED = SourceStatus.VERIFIED.value


@pytest.fixture
async def duplicates_allowed(db_session) -> None:
    """The database as the merge found it, before the index that forbids it.

    Two verified rows for one board cannot be written any more, so a test of
    the repair has to take the index away first. DDL is transactional in
    Postgres: the drop is undone by the same rollback that undoes the rows.
    """
    await db_session.execute(text("DROP INDEX uq_companies_verified_board"))


async def _company(session, name: str, slug: str, *, ats: str = "greenhouse", age_days: int = 0):
    company = Company(
        name=name,
        ats_type=ats,
        slug=slug,
        source_status=VERIFIED,
        source_evidence={"method": "discovery_from_url"},
        created_at=NOW - timedelta(days=age_days),
    )
    session.add(company)
    await session.flush()
    return company


async def _posting(session, company: Company, external_id: str) -> Posting:
    posting = Posting(
        company_id=company.id,
        external_id=external_id,
        url=f"https://boards.greenhouse.io/x/jobs/{external_id}",
        title=f"Engineer {external_id}",
    )
    session.add(posting)
    await session.flush()
    return posting


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


async def _titles(session, company: Company) -> set[str]:
    rows = await session.scalars(
        select(Posting.external_id).where(Posting.company_id == company.id)
    )
    return set(rows.all())


# --- which row keeps the board ------------------------------------------------


@pytest.mark.parametrize(
    ("slug", "names", "kept"),
    [
        ("vercel", ["ZEIT", "Vercel"], "Vercel"),
        ("elastic", ["Elastic.co", "Elastic"], "Elastic"),
        ("flexport", ["Deliverr", "Flexport"], "Flexport"),
        ("goteleport", ["Gravitational", "Teleport"], "Teleport"),
        ("arkestroinc", ["Bid Ops", "Arkestro"], "Arkestro"),
        ("frontcareers", ["Front App", "Front"], "Front"),
        ("twelve", ["Twelve Labs", "Twelve"], "Twelve"),
    ],
)
def test_the_row_named_like_the_board_keeps_it(slug: str, names: list[str], kept: str) -> None:
    """The eleven pairs as found. The name is what the owner reads on a card."""
    rows = [Company(id=uuid.uuid4(), name=name, slug=slug, created_at=NOW) for name in names]

    assert boards.keeper_of(rows).name == kept


def test_when_the_names_say_nothing_the_older_row_keeps_it() -> None:
    """Replit and Repl.it are the same word once the dot is gone."""
    older = Company(
        id=uuid.uuid4(), name="Replit", slug="replit", created_at=NOW - timedelta(days=20)
    )
    newer = Company(id=uuid.uuid4(), name="Repl.it", slug="replit", created_at=NOW)

    assert boards.keeper_of([newer, older]) is older


# --- merging what is already there --------------------------------------------


async def test_the_copies_go_and_the_originals_stay(db_session, duplicates_allowed) -> None:
    vercel = await _company(db_session, "Vercel", "vercel", age_days=40)
    zeit = await _company(db_session, "ZEIT", "vercel")
    for company in (vercel, zeit):
        await _posting(db_session, company, "101")
        await _posting(db_session, company, "102")

    report = await boards.merge(db_session, now=NOW, apply=True)

    assert (report.boards, report.postings_deleted) == (1, 2)
    assert await _titles(db_session, vercel) == {"101", "102"}
    assert await _titles(db_session, zeit) == set()


async def test_a_dry_run_changes_nothing_and_reports_the_same(
    db_session, duplicates_allowed
) -> None:
    vercel = await _company(db_session, "Vercel", "vercel", age_days=40)
    zeit = await _company(db_session, "ZEIT", "vercel")
    for company in (vercel, zeit):
        await _posting(db_session, company, "101")

    report = await boards.merge(db_session, now=NOW, apply=False)

    assert (report.boards, report.postings_deleted, report.companies_set_aside) == (1, 1, 1)
    assert await _titles(db_session, zeit) == {"101"}
    assert zeit.source_status == VERIFIED


async def test_the_other_row_is_set_aside_with_the_reason(db_session, duplicates_allowed) -> None:
    """Kept as a row, with what it was: the registry records evidence, not deletions."""
    vercel = await _company(db_session, "Vercel", "vercel", age_days=40)
    zeit = await _company(db_session, "ZEIT", "vercel")

    await boards.merge(db_session, now=NOW, apply=True)

    assert zeit.source_status == SourceStatus.FAILED.value
    assert zeit.slug is None, "the board is the keeper's; two rows naming it is the defect"
    assert "Vercel" in (zeit.discovery_failure or "")
    assert zeit.source_evidence["method"] == boards.DUPLICATE_METHOD
    assert zeit.source_evidence["duplicate_of"] == str(vercel.id)
    assert zeit.source_evidence["slug"] == "vercel"
    assert zeit.source_evidence["previous"] == {"method": "discovery_from_url"}
    assert vercel.source_status == VERIFIED and vercel.slug == "vercel"


async def test_a_score_only_the_copy_had_moves_to_the_original(
    db_session, duplicates_allowed
) -> None:
    """Flexport's 58 postings had no scores and Deliverr's copies had ten."""
    _, profile = await _owner(db_session)
    flexport = await _company(db_session, "Flexport", "flexport")
    deliverr = await _company(db_session, "Deliverr", "flexport")
    original = await _posting(db_session, flexport, "7")
    copy = await _posting(db_session, deliverr, "7")
    db_session.add(Match(profile_id=profile.id, posting_id=copy.id, score=0.42))
    await db_session.flush()

    report = await boards.merge(db_session, now=NOW, apply=True)

    scores = (
        await db_session.scalars(select(Match.score).where(Match.posting_id == original.id))
    ).all()
    assert scores == [0.42]
    assert report.matches_moved == 1


async def test_a_copy_the_owner_acted_on_is_left_alone(db_session, duplicates_allowed) -> None:
    """Deleting it would take the application's posting with it (`retention.py`)."""
    candidate, profile = await _owner(db_session)
    vercel = await _company(db_session, "Vercel", "vercel", age_days=40)
    zeit = await _company(db_session, "ZEIT", "vercel")
    await _posting(db_session, vercel, "101")
    applied = await _posting(db_session, zeit, "101")
    db_session.add(
        Application(
            candidate_id=candidate.id, profile_id=profile.id, posting_id=applied.id, url=applied.url
        )
    )
    await db_session.flush()

    report = await boards.merge(db_session, now=NOW, apply=True)

    assert (report.postings_deleted, report.kept_for_owner) == (0, 1)
    assert await _titles(db_session, zeit) == {"101"}


async def test_a_posting_only_the_other_row_had_is_moved_not_lost(
    db_session, duplicates_allowed
) -> None:
    vercel = await _company(db_session, "Vercel", "vercel", age_days=40)
    zeit = await _company(db_session, "ZEIT", "vercel")
    await _posting(db_session, vercel, "101")
    await _posting(db_session, zeit, "999")

    report = await boards.merge(db_session, now=NOW, apply=True)

    assert report.postings_moved == 1
    assert await _titles(db_session, vercel) == {"101", "999"}


async def test_two_companies_on_different_boards_are_not_touched(db_session) -> None:
    """The same slug on another ATS is another board."""
    await _company(db_session, "Sift", "sift", ats="ashby")
    await _company(db_session, "Sift Media", "sift", ats="greenhouse")

    report = await boards.merge(db_session, now=NOW, apply=True)

    assert report.boards == 0


# --- so that it cannot happen again -------------------------------------------


async def test_the_holder_of_a_board_can_be_asked_for(db_session) -> None:
    vercel = await _company(db_session, "Vercel", "vercel")
    zeit = Company(name="ZEIT", source_status=SourceStatus.HINT.value)
    db_session.add(zeit)
    await db_session.flush()

    assert await boards.holder_of(db_session, "greenhouse", "vercel", other_than=zeit.id) is vercel
    assert await boards.holder_of(db_session, "greenhouse", "vercel", other_than=vercel.id) is None
    assert await boards.holder_of(db_session, "ashby", "vercel", other_than=zeit.id) is None


async def test_the_database_refuses_a_second_verified_row_for_a_board(db_session) -> None:
    """Two discovery tasks can resolve the same board at once, each before the
    other has committed, and neither then sees a holder. The index is what
    still holds when the check in the handler has been raced past."""
    await _company(db_session, "Vercel", "vercel")

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _company(db_session, "ZEIT", "vercel")
