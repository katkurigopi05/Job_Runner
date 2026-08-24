"""Tailoring ahead of the queue.

Two properties carry the weight here, and both are about what the batch
refuses to do.

**It stops with calls in hand.** A run that spends the last of the day's
allowance halfway through leaves a half-tailored queue that looks exactly like
a whole one, and the owner sends base résumés believing they were tailored.

**It does not attach a résumé that was not tailored.** When the guard refuses
every rewrite the output is the source document. Storing it would report a
tailored résumé that is identical to the profile's own.
"""

from __future__ import annotations

import uuid

import pytest

from packages.core.models import Candidate, Match, Posting, Profile, Project, Resume, User
from packages.tailor import batch

pytestmark = pytest.mark.asyncio

RESUME = {
    "contact": {"name": "Fixture Owner", "email": "fixture@example.com", "links": []},
    "preamble": [],
    "sections": {"experience": ["Built backend services in Python.", "Ran the on-call rotation."]},
    "raw_lines": ["Built backend services in Python.", "Ran the on-call rotation."],
}


async def _owner(session) -> Profile:
    suffix = uuid.uuid4().hex[:8]
    user = User(email=f"o-{suffix}@example.com")
    session.add(user)
    await session.flush()
    candidate = Candidate(user_id=user.id, name="Owner", email=f"o-{suffix}@example.com")
    session.add(candidate)
    await session.flush()
    resume = Resume(
        candidate_id=candidate.id, version=1, storage_ref=f"r/{suffix}.txt", parsed_json=RESUME
    )
    session.add(resume)
    await session.flush()
    profile = Profile(candidate_id=candidate.id, label="default", base_resume_id=resume.id)
    session.add(profile)
    await session.flush()
    return profile


async def _match(session, profile: Profile, *, decision: str | None, score: float = 0.5) -> Match:
    suffix = uuid.uuid4().hex[:8]
    posting = Posting(
        url=f"https://example.com/{suffix}",
        title="Backend Engineer",
        description_raw="We want Python and on-call experience.",
    )
    session.add(posting)
    await session.flush()
    match = Match(profile_id=profile.id, posting_id=posting.id, score=score, decision=decision)
    session.add(match)
    await session.flush()
    return match


async def test_only_postings_the_owner_kept_are_tailored(db_session) -> None:
    """The gate that makes the quota arithmetic work.

    Tailoring the whole feed is five calls times every scored posting. The
    owner applies to the ones they picked, so those are the ones worth
    spending an allowance on.
    """
    profile = await _owner(db_session)
    await _match(db_session, profile, decision="interested")
    await _match(db_session, profile, decision="skipped")
    await _match(db_session, profile, decision=None)

    waiting = await batch.pending(db_session, str(profile.id))

    assert len(waiting) == 1
    assert waiting[0][0].decision == "interested"


async def test_the_strongest_matches_go_first(db_session) -> None:
    """A run cut short by the quota should have spent it on the best ones."""
    profile = await _owner(db_session)
    await _match(db_session, profile, decision="interested", score=0.20)
    await _match(db_session, profile, decision="interested", score=0.90)

    waiting = await batch.pending(db_session, str(profile.id))

    assert [round(m.score, 2) for m, _ in waiting] == [0.90, 0.20]


async def test_an_already_tailored_posting_is_not_redone(db_session) -> None:
    profile = await _owner(db_session)
    match = await _match(db_session, profile, decision="interested")
    match.tailored_resume_id = profile.base_resume_id
    await db_session.flush()

    assert await batch.pending(db_session, str(profile.id)) == []


async def test_a_closed_posting_is_not_tailored(db_session) -> None:
    """Spending calls on a job nobody can apply to is the clearest waste."""
    from datetime import UTC, datetime

    profile = await _owner(db_session)
    match = await _match(db_session, profile, decision="interested")
    posting = await db_session.get(Posting, match.posting_id)
    posting.closed_at = datetime.now(UTC)
    await db_session.flush()

    assert await batch.pending(db_session, str(profile.id)) == []


async def test_a_run_with_nothing_pending_reports_rather_than_erroring(db_session) -> None:
    from packages.llm.provider import StubProvider

    profile = await _owner(db_session)

    result = await batch.run(db_session, StubProvider(), profile_id=str(profile.id))

    assert result.tailored == 0
    assert result.stopped_reason is None
    assert "0 tailored" in result.summary()


async def test_the_quota_margin_stops_the_run_before_the_last_call(db_session, monkeypatch) -> None:
    """Stopping early is the whole point.

    Discovering the limit by hitting it leaves a queue the owner cannot tell
    apart from a finished one.
    """
    from packages.llm.provider import StubProvider

    profile = await _owner(db_session)
    await _match(db_session, profile, decision="interested")

    monkeypatch.setattr(batch.quota, "remaining", lambda provider: batch.QUOTA_MARGIN)

    result = await batch.run(db_session, StubProvider(), profile_id=str(profile.id))

    assert result.tailored == 0
    assert result.stopped_reason is not None
    assert "calls left today" in result.stopped_reason


async def test_relevant_github_project_is_published_for_a_missing_resume_skill(
    db_session, monkeypatch
) -> None:
    """GitHub can fill a truthful Projects gap, never an employer-history gap."""
    from packages.llm.provider import StubProvider

    profile = await _owner(db_session)
    match = await _match(db_session, profile, decision="interested")
    posting = await db_session.get(Posting, match.posting_id)
    posting.description_raw = "Python engineer with time-series forecasting experience."
    db_session.add(
        Project(
            candidate_id=profile.candidate_id,
            source="github",
            external_id=uuid.uuid4().hex,
            name="forecasting-lab",
            full_name="owner/forecasting-lab",
            url="https://github.com/owner/forecasting-lab",
            description="Demand forecasting experiments",
            language="Python",
            topics_json=["time-series", "pandas"],
        )
    )
    await db_session.flush()

    captured: dict[str, object] = {}

    async def fake_publish(session, **kwargs):
        captured.update(kwargs)
        return await session.get(Resume, profile.base_resume_id)

    monkeypatch.setattr(batch, "publish_tailored", fake_publish)
    monkeypatch.setattr(batch.quota, "remaining", lambda provider: None)

    result = await batch.run(db_session, StubProvider(), profile_id=str(profile.id))

    projects = captured["projects"]
    assert [project.name for project in projects] == ["forecasting-lab"]
    assert result.tailored == 1
    assert "1 GitHub project" in result.per_posting[0][1]


async def test_github_skill_does_not_license_rewriting_an_employer_bullet(
    db_session, monkeypatch
) -> None:
    """Project evidence stays attributed to the project that proves it."""
    from packages.llm.provider import StubProvider

    profile = await _owner(db_session)
    match = await _match(db_session, profile, decision="interested")
    posting = await db_session.get(Posting, match.posting_id)
    posting.description_raw = "Python engineer with time-series forecasting experience."
    db_session.add(
        Project(
            candidate_id=profile.candidate_id,
            source="github",
            external_id=uuid.uuid4().hex,
            name="forecasting-lab",
            full_name="owner/forecasting-lab",
            url="https://github.com/owner/forecasting-lab",
            description="Time-series forecasting experiments",
            language="Python",
            topics_json=["time-series"],
        )
    )
    await db_session.flush()

    captured: dict[str, object] = {}

    async def fake_publish(session, **kwargs):
        captured.update(kwargs)
        return await session.get(Resume, profile.base_resume_id)

    monkeypatch.setattr(batch, "publish_tailored", fake_publish)
    monkeypatch.setattr(batch.quota, "remaining", lambda provider: None)
    provider = StubProvider(
        {
            "Built backend services": (
                "Built backend services in Python for time-series forecasting."
            )
        }
    )

    await batch.run(db_session, provider, profile_id=str(profile.id))

    rewrites = captured["result"].bullets
    assert rewrites[0].tailored == "Built backend services in Python."
