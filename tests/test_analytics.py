"""The feedback loop — does the score predict anything, and who has gone quiet.

These reports exist to be *believed*, which makes the failure mode different
from the rest of the project. A crawler that breaks stops producing postings
and somebody notices. A funnel report that divides by the wrong denominator
produces a confident number nobody can tell is wrong, and the owner changes
how they search because of it.

So the cases pinned here are the ones that produce a plausible lie: rates over
an empty denominator, a verdict drawn from three applications, and silence
counted on an application the employer already answered.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from packages.analytics import cadence, digest, funnel
from packages.core.enums import ApplicationStatus, Outcome
from packages.core.models import (
    Application,
    Candidate,
    InboundMessage,
    Match,
    Posting,
    Profile,
    User,
)

pytestmark = pytest.mark.asyncio


async def _owner(session) -> tuple[Candidate, Profile]:
    suffix = uuid.uuid4().hex[:8]
    user = User(email=f"owner-{suffix}@example.com")
    session.add(user)
    await session.flush()
    candidate = Candidate(user_id=user.id, name="Owner", email=f"owner-{suffix}@example.com")
    session.add(candidate)
    await session.flush()
    profile = Profile(candidate_id=candidate.id, label="default")
    session.add(profile)
    await session.flush()
    return candidate, profile


async def _application(
    session,
    candidate: Candidate,
    profile: Profile,
    *,
    score: float | None = None,
    status: str = ApplicationStatus.SUBMITTED.value,
    outcome: str | None = None,
    created_days_ago: int = 0,
    outcome_days_after: int | None = None,
) -> Application:
    suffix = uuid.uuid4().hex[:8]
    posting = Posting(url=f"https://example.com/{suffix}", title="Engineer")
    session.add(posting)
    await session.flush()

    if score is not None:
        session.add(Match(profile_id=profile.id, posting_id=posting.id, score=score))

    created = datetime.now(UTC) - timedelta(days=created_days_ago)
    application = Application(
        candidate_id=candidate.id,
        profile_id=profile.id,
        posting_id=posting.id,
        url=f"https://example.com/{suffix}/apply",
        status=status,
        outcome=outcome,
        outcome_at=(
            created + timedelta(days=outcome_days_after) if outcome_days_after is not None else None
        ),
    )
    session.add(application)
    await session.flush()
    # created_at has a server default, so it is set after flush, not before.
    application.created_at = created
    await session.flush()
    return application


# --------------------------------------------------------------------------
# Funnel
# --------------------------------------------------------------------------


async def test_rates_are_none_not_zero_when_nothing_was_submitted(db_session) -> None:
    """An empty denominator is unknown, not zero.

    Reporting 0% on a fresh install reads as "every employer ignored you",
    which is a specific and discouraging claim about data that does not exist.
    """
    candidate, profile = await _owner(db_session)
    await _application(db_session, candidate, profile, status=ApplicationStatus.QUEUED.value)

    report = await funnel.build(db_session, profile_id=str(profile.id))

    assert report.stages.submitted == 0
    assert report.stages.answer_rate is None
    assert report.stages.engagement_rate is None


async def test_acknowledgement_is_not_engagement(db_session) -> None:
    """An automated "we received your application" is not a reply.

    Counting it would make every bucket look successful, which is exactly the
    number this report exists to avoid producing.
    """
    candidate, profile = await _owner(db_session)
    await _application(
        db_session, candidate, profile, score=0.9, outcome=Outcome.ACKNOWLEDGED.value
    )

    report = await funnel.build(db_session, profile_id=str(profile.id))

    assert report.stages.submitted == 1
    assert report.stages.engaged == 0
    assert report.stages.answered == 0


async def test_a_rejection_counts_as_answered_but_not_engaged(db_session) -> None:
    candidate, profile = await _owner(db_session)
    await _application(db_session, candidate, profile, score=0.9, outcome=Outcome.REJECTED.value)

    report = await funnel.build(db_session, profile_id=str(profile.id))

    assert report.stages.answered == 1
    assert report.stages.engaged == 0


async def test_no_verdict_from_a_handful_of_applications(db_session) -> None:
    """Three applications is not evidence that the scorer works.

    The whole point of `score_tracks_outcome` is to stay None until there is
    something to say. A confident verdict here is worse than silence, because
    the owner would act on it.
    """
    candidate, profile = await _owner(db_session)
    await _application(db_session, candidate, profile, score=0.9, outcome=Outcome.INTERVIEW.value)
    await _application(db_session, candidate, profile, score=0.2)
    await _application(db_session, candidate, profile, score=0.2)

    report = await funnel.build(db_session, profile_id=str(profile.id))

    assert report.score_tracks_outcome is None
    assert not any(bucket.is_meaningful for bucket in report.buckets)


async def test_a_verdict_appears_once_both_buckets_are_populated(db_session) -> None:
    candidate, profile = await _owner(db_session)
    for _ in range(5):
        await _application(
            db_session, candidate, profile, score=0.9, outcome=Outcome.INTERVIEW.value
        )
    for _ in range(5):
        await _application(db_session, candidate, profile, score=0.2)

    report = await funnel.build(db_session, profile_id=str(profile.id))

    assert report.score_tracks_outcome is True


async def test_applications_without_a_score_are_counted_separately(db_session) -> None:
    """Applying by URL leaves no Match row.

    Folding those into the lowest bucket would understate its performance with
    applications that were never scored at all.
    """
    candidate, profile = await _owner(db_session)
    await _application(db_session, candidate, profile, score=None)

    report = await funnel.build(db_session, profile_id=str(profile.id))

    assert report.unscored == 1
    assert sum(bucket.applications for bucket in report.buckets) == 0


# --------------------------------------------------------------------------
# Cadence
# --------------------------------------------------------------------------


async def test_an_answered_application_is_not_silent(db_session) -> None:
    candidate, profile = await _owner(db_session)
    await _application(
        db_session,
        candidate,
        profile,
        created_days_ago=30,
        outcome=Outcome.REJECTED.value,
        outcome_days_after=3,
    )

    report = await cadence.silence(db_session, profile_id=str(profile.id))

    assert report.silent == []


async def test_silence_is_reported_oldest_first(db_session) -> None:
    candidate, profile = await _owner(db_session)
    await _application(db_session, candidate, profile, created_days_ago=20)
    await _application(db_session, candidate, profile, created_days_ago=60)

    report = await cadence.silence(db_session, profile_id=str(profile.id))

    assert [item.days_since for item in report.silent] == [60, 20]
    assert len(report.due) == 1
    assert len(report.stale) == 1


async def test_a_recent_application_is_not_yet_silent(db_session) -> None:
    candidate, profile = await _owner(db_session)
    await _application(db_session, candidate, profile, created_days_ago=3)

    report = await cadence.silence(db_session, profile_id=str(profile.id))

    assert report.silent == []


async def test_latency_splits_rejections_from_engagement(db_session) -> None:
    """A rejection and an interview invitation travel at different speeds.

    Averaging them describes neither, and the follow-up threshold derived from
    the blend would be wrong for both.
    """
    candidate, profile = await _owner(db_session)
    for days in (2, 4, 6):
        await _application(
            db_session,
            candidate,
            profile,
            created_days_ago=60,
            outcome=Outcome.REJECTED.value,
            outcome_days_after=days,
        )
    for days in (20, 30):
        await _application(
            db_session,
            candidate,
            profile,
            created_days_ago=60,
            outcome=Outcome.INTERVIEW.value,
            outcome_days_after=days,
        )

    report = await cadence.latency(db_session, profile_id=str(profile.id))

    assert report.samples == 5
    assert report.median_rejection_days == 4
    assert report.median_engagement_days == 25


async def test_no_threshold_is_suggested_without_enough_history(db_session) -> None:
    candidate, profile = await _owner(db_session)
    await _application(
        db_session,
        candidate,
        profile,
        created_days_ago=30,
        outcome=Outcome.REJECTED.value,
        outcome_days_after=5,
    )

    report = await cadence.latency(db_session, profile_id=str(profile.id))

    assert report.samples == 1
    assert report.suggested_silent_after_days is None


# --------------------------------------------------------------------------
# Digest
# --------------------------------------------------------------------------


async def test_a_quiet_week_is_named_rather_than_left_as_zeroes(db_session) -> None:
    """Six zeroes usually means the crawler stopped, not that the market did."""
    _candidate, profile = await _owner(db_session)

    report = await digest.build(db_session, profile_id=str(profile.id))

    assert report.quiet_week is True


async def test_the_digest_counts_only_the_window(db_session) -> None:
    candidate, profile = await _owner(db_session)
    await _application(db_session, candidate, profile, created_days_ago=2)
    await _application(db_session, candidate, profile, created_days_ago=40)

    report = await digest.build(db_session, profile_id=str(profile.id))

    assert report.applications_created == 1
    # The funnel is all-time on purpose — a week is far too little to read a
    # conversion rate from.
    assert report.funnel.stages.total == 2


# --------------------------------------------------------------------------
# An outcome added later, and an outcome that was overwritten
# --------------------------------------------------------------------------
#
# Found on 2026-10-12 by asking two questions another project's tests ask of
# its own reports (MadsLorentzen/ai-job-search, MIT): is every status in one
# authoritative list, and is a funnel counted from what happened or from
# where things stand now.


async def test_every_outcome_is_placed_on_purpose() -> None:
    """`Outcome.ASSESSMENT` was added to the enum and to none of the sets
    here, so an employer sending a timed exercise had not answered, had not
    engaged, and was owed a follow-up for its silence. An outcome added next
    fails this until somebody says which it is."""
    placed = [funnel.ENGAGED, funnel.NOT_AN_ANSWER, frozenset({Outcome.REJECTED.value})]

    for outcome in Outcome:
        homes = [group for group in placed if outcome.value in group]
        assert len(homes) == 1, f"{outcome.value} is in {len(homes)} of the three groups"
    assert funnel.ENGAGED | {Outcome.REJECTED.value} == funnel.ANSWERED
    # Silence has ended wherever the funnel says somebody answered.
    assert funnel.ANSWERED <= cadence._ANSWERED


async def test_an_assessment_is_an_answer_and_an_engagement(db_session) -> None:
    candidate, profile = await _owner(db_session)
    await _application(db_session, candidate, profile, outcome=Outcome.ASSESSMENT.value)

    report = await funnel.build(db_session, profile_id=str(profile.id))

    assert report.stages.answered == 1
    assert report.stages.engaged == 1


async def test_an_application_with_an_assessment_is_not_silent(db_session) -> None:
    """It has a deadline on it. Telling the owner nobody replied is the
    opposite of what they need to hear."""
    candidate, profile = await _owner(db_session)
    await _application(
        db_session,
        candidate,
        profile,
        created_days_ago=30,
        outcome=Outcome.ASSESSMENT.value,
        outcome_days_after=3,
    )

    report = await cadence.silence(db_session, profile_id=str(profile.id))

    assert report.silent == []


async def test_an_assessment_is_timed_with_the_other_engagements(db_session) -> None:
    candidate, profile = await _owner(db_session)
    for days in (8, 12):
        await _application(
            db_session,
            candidate,
            profile,
            created_days_ago=60,
            outcome=Outcome.ASSESSMENT.value,
            outcome_days_after=days,
        )

    report = await cadence.latency(db_session, profile_id=str(profile.id))

    assert report.median_engagement_days == 10


async def _message(session, application: Application, classification: str, link: str) -> None:
    session.add(
        InboundMessage(
            candidate_id=application.candidate_id,
            application_id=application.id,
            from_addr="recruiter@example.com",
            classification=classification,
            link_method=link,
        )
    )
    await session.flush()


async def test_an_interview_that_ended_in_rejection_was_still_an_interview(db_session) -> None:
    """`outcome` holds the latest and most final thing an employer said, and
    a rejection outranks an interview. Most interviews end in one. Counted
    from `outcome` alone, the applications that got furthest were the ones
    the engagement rate lost, in the report that asks whether a high score
    earns a reply."""
    candidate, profile = await _owner(db_session)
    application = await _application(
        db_session, candidate, profile, score=0.9, outcome=Outcome.REJECTED.value
    )
    await _message(db_session, application, "interview", "alias")
    await _message(db_session, application, "rejection", "alias")

    report = await funnel.build(db_session, profile_id=str(profile.id))

    assert report.stages.answered == 1
    assert report.stages.engaged == 1
    bucket = next(b for b in report.buckets if b.label == "0.80–1.00")
    assert (bucket.answered, bucket.engaged) == (1, 1)


async def test_a_message_guessed_onto_an_application_proves_nothing(db_session) -> None:
    """Only an exact alias may conclude anything (`inbox/route.py`). A message
    matched on its sender and wording is a guess about which application it
    belongs to, and the funnel does not count a guess."""
    candidate, profile = await _owner(db_session)
    application = await _application(db_session, candidate, profile, outcome=Outcome.REJECTED.value)
    await _message(db_session, application, "interview", "inferred")

    report = await funnel.build(db_session, profile_id=str(profile.id))

    assert report.stages.engaged == 0


async def test_a_rejection_alone_is_still_not_an_engagement(db_session) -> None:
    candidate, profile = await _owner(db_session)
    application = await _application(db_session, candidate, profile, outcome=Outcome.REJECTED.value)
    await _message(db_session, application, "acknowledgement", "alias")
    await _message(db_session, application, "rejection", "alias")

    report = await funnel.build(db_session, profile_id=str(profile.id))

    assert (report.stages.answered, report.stages.engaged) == (1, 0)
