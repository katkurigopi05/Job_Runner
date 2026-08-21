"""Re-scoring the feed on demand.

The reason this exists is a gap rather than a feature: scoring rides on a
crawl, and the crawl returns early when the sweep emitted nothing. So the one
moment the owner most wants a re-score — they just replaced the résumé the
score is computed from — is the moment nothing fires.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from packages.core.models import Candidate, Posting, Profile, Resume, User
from packages.matching.rescore import rescore

pytestmark = pytest.mark.asyncio


async def _owner(session, *, resume_lines: list[str], location: str = "San Francisco, CA"):
    suffix = uuid.uuid4().hex[:8]
    user = User(email=f"owner-{suffix}@example.com")
    session.add(user)
    await session.flush()

    candidate = Candidate(user_id=user.id, name="Owner", email=f"o-{suffix}@example.com")
    session.add(candidate)
    await session.flush()

    resume = Resume(
        candidate_id=candidate.id,
        version=1,
        storage_ref=f"resumes/{candidate.id}/v1/resume.txt",
        parsed_json={"raw_lines": resume_lines},
    )
    session.add(resume)
    await session.flush()

    profile = Profile(
        candidate_id=candidate.id,
        label=f"p-{suffix}",
        base_resume_id=resume.id,
        location=location,
    )
    session.add(profile)
    await session.flush()
    return profile, resume


async def _posting(session, *, title: str, body: str, location: str = "San Francisco, CA"):
    posting = Posting(
        url=f"https://boards.greenhouse.io/acme/jobs/{uuid.uuid4().hex[:10]}",
        title=title,
        location=location,
        description_raw=body,
    )
    session.add(posting)
    await session.flush()
    return posting


async def test_replacing_the_resume_moves_the_score(db_session) -> None:
    """The whole point. Nothing else in the project makes this happen."""
    profile, resume = await _owner(
        db_session, resume_lines=["Backend engineer.", "Python and PostgreSQL."]
    )
    await _posting(
        db_session,
        title="Senior iOS Engineer",
        body="Swift, UIKit, CoreData and iOS release engineering.",
    )

    first = await rescore(db_session, label=profile.label)
    assert first.profiles[0].created == 1
    before = first.profiles[0].after_top[0][0]

    # The owner uploads a résumé that actually matches the posting.
    resume.parsed_json = {
        "raw_lines": ["iOS engineer.", "Swift, UIKit, CoreData, iOS release engineering."]
    }
    await db_session.flush()

    second = await rescore(db_session, label=profile.label)
    assert second.profiles[0].moved == 1, "the score should have moved"
    assert second.profiles[0].created == 0, "the match already existed"
    assert second.profiles[0].after_top[0][0] > before


async def test_an_excluded_posting_is_not_counted_as_a_created_match(db_session) -> None:
    """Scored is not written.

    `score_and_store` skips storing a posting that failed a hard filter, so
    counting every scored posting as a created Match reported 9,069 new rows
    on a run against the real database that wrote none.
    """
    profile, _ = await _owner(
        db_session, resume_lines=["Backend engineer."], location="San Francisco, CA"
    )
    await _posting(db_session, title="Backend Engineer", body="Python.", location="Berlin, Germany")

    report = await rescore(db_session, label=profile.label)
    entry = report.profiles[0]

    assert entry.scored == 1
    assert entry.excluded == 1
    assert entry.created == 0


async def test_a_closed_posting_is_not_scored(db_session) -> None:
    profile, _ = await _owner(db_session, resume_lines=["Backend engineer."])
    posting = await _posting(db_session, title="Backend Engineer", body="Python.")
    posting.closed_at = datetime.now(UTC)
    await db_session.flush()

    report = await rescore(db_session, label=profile.label)
    assert report.postings == 0
    assert report.profiles == []


async def test_only_the_named_profile_is_rescored(db_session) -> None:
    first, _ = await _owner(db_session, resume_lines=["Backend engineer."])
    second, _ = await _owner(db_session, resume_lines=["Data engineer."])
    await _posting(db_session, title="Backend Engineer", body="Python and PostgreSQL.")

    report = await rescore(db_session, label=first.label)

    assert [entry.label for entry in report.profiles] == [first.label]
    assert second.label not in {entry.label for entry in report.profiles}


async def test_nothing_is_committed_so_a_dry_run_needs_no_second_path(db_session) -> None:
    """`rescore` flushes but never commits; the CLI's --dry-run just rolls back."""
    profile, _ = await _owner(db_session, resume_lines=["Backend engineer."])
    await _posting(db_session, title="Backend Engineer", body="Python and PostgreSQL.")

    await rescore(db_session, label=profile.label)
    assert db_session.in_transaction()
