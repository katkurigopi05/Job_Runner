"""The doorbell rings again when an application parks a second time.

`notify_if_parked` promised this from the day it was written — "an application
that parks, resumes and parks again does ring twice" — and did not do it. The
check asked whether the application had *ever* been notified for this reason,
and `ParkReason` is derived from the status alone, so the second park was
silent whenever it parked the same way as the first.

That is the common case, not an edge one. §6 has `needs_review ──approve──>
running`, and approving re-enters the pipeline from the top: fresh page,
re-`goto`, re-`enumerate_fields`. The `report.is_complete` park sits *above*
the `owner_approved` short-circuit in `apply_job._run_pipeline`, so a form that
still has an unanswered required question parks again — and the owner, who
approved it and heard nothing more, has every reason to believe it went out.

`test_parking_a_second_time_rings_again` in `test_notify.py` looked like it
covered this and did not: it parks at `needs_review` and then at
`failed[manual_completion_required]`, two *different* reasons, so it only ever
exercised the case that already worked.

These tests commit for real rather than using `db_session`. The boundary the
fix keys on is the last transition into the current status, and `at` defaults
to the transaction clock — inside one transaction every event would share a
timestamp and none of this would be visible. `run.py` commits the task and
*then* calls `notify_if_parked`, so in production the two always land in
different transactions; the fixture reproduces that rather than assuming it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.enums import ApplicationStatus, EventType, FailureReason
from packages.core.models import Application, ApplicationEvent, Candidate, Profile, Resume, User
from packages.core.notify import ParkReason, notify_if_parked
from packages.core.state import transition

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _log_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped default sends nothing; `log` is delivery that goes nowhere."""
    monkeypatch.setenv("NOTIFY_BACKENDS", "log")
    from packages.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def queued(committing_sessionmaker) -> AsyncIterator[uuid.UUID]:
    """A committed application sitting in `queued`, ready to be driven."""
    suffix = uuid.uuid4().hex[:8]
    async with committing_sessionmaker() as session:
        user = User(email=f"owner-{suffix}@example.com")
        session.add(user)
        await session.flush()

        candidate = Candidate(
            user_id=user.id, name="Test Owner", email=f"owner-{suffix}@example.com"
        )
        session.add(candidate)
        await session.flush()

        resume = Resume(
            candidate_id=candidate.id,
            version=1,
            storage_ref=f"resumes/{suffix}.pdf",
            parsed_json={},
            is_default=True,
        )
        session.add(resume)
        await session.flush()

        profile = Profile(candidate_id=candidate.id, label="default", base_resume_id=resume.id)
        session.add(profile)
        await session.flush()

        application = Application(
            candidate_id=candidate.id,
            profile_id=profile.id,
            url=f"https://boards.greenhouse.io/acme/jobs/{suffix}",
            ats="greenhouse",
            status=ApplicationStatus.QUEUED.value,
        )
        session.add(application)
        await session.commit()
        yield application.id


async def _move(
    maker,
    application_id: uuid.UUID,
    to: ApplicationStatus,
    *,
    failure_reason: FailureReason | None = None,
) -> None:
    """One committed transition, exactly as the worker makes it."""
    async with maker() as session:
        application = await session.get(Application, application_id)
        assert application is not None
        await transition(session, application, to, failure_reason=failure_reason)
        await session.commit()


async def _ring(maker, application_id: uuid.UUID) -> ParkReason | None:
    """Ring in its own transaction, which is where `run.py` rings from."""
    async with maker() as session:
        return await notify_if_parked(session, application_id)


async def _notifications(session: AsyncSession, application_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(ApplicationEvent)
            .where(
                ApplicationEvent.application_id == application_id,
                ApplicationEvent.type == EventType.NOTIFIED.value,
            )
        )
    ) or 0


async def test_a_second_park_for_the_same_reason_rings_again(
    committing_sessionmaker, queued: uuid.UUID
) -> None:
    """Approve, re-run, park again — a second thing to do, so a second ring.

    This is the test the module was missing. Reverting the fix turns it red.
    """
    await _move(committing_sessionmaker, queued, ApplicationStatus.RUNNING)
    await _move(committing_sessionmaker, queued, ApplicationStatus.NEEDS_REVIEW)
    assert await _ring(committing_sessionmaker, queued) is ParkReason.REVIEW

    # The owner approves. The pipeline restarts from the top and finds the
    # form still carries a question it cannot answer, so it parks again.
    await _move(committing_sessionmaker, queued, ApplicationStatus.RUNNING)
    await _move(committing_sessionmaker, queued, ApplicationStatus.NEEDS_REVIEW)

    assert await _ring(committing_sessionmaker, queued) is ParkReason.REVIEW, (
        "the second park was silent — the owner approved it and heard nothing more"
    )

    async with committing_sessionmaker() as session:
        assert await _notifications(session, queued) == 2


async def test_a_rerun_of_one_park_still_rings_only_once(
    committing_sessionmaker, queued: uuid.UUID
) -> None:
    """The at-least-once guarantee this check exists for, unchanged.

    No new transition happens when a task is retried, so the boundary does not
    move and the delivery already recorded against it still counts.
    """
    await _move(committing_sessionmaker, queued, ApplicationStatus.RUNNING)
    await _move(committing_sessionmaker, queued, ApplicationStatus.NEEDS_REVIEW)

    assert await _ring(committing_sessionmaker, queued) is ParkReason.REVIEW
    assert await _ring(committing_sessionmaker, queued) is None, "rang twice for one park"
    assert await _ring(committing_sessionmaker, queued) is None

    async with committing_sessionmaker() as session:
        assert await _notifications(session, queued) == 1


async def test_a_second_otp_park_rings_again(committing_sessionmaker, queued: uuid.UUID) -> None:
    """A resent code is the same shape, and the one with a deadline attached.

    §6's `needs_otp ──otp──> running` edge resumes the run; if the site asks
    again the application parks again, and the owner has to know a second code
    is wanted. Nothing in the tracker distinguishes a fresh wait from a stale
    one.
    """
    await _move(committing_sessionmaker, queued, ApplicationStatus.RUNNING)
    await _move(committing_sessionmaker, queued, ApplicationStatus.NEEDS_OTP)
    assert await _ring(committing_sessionmaker, queued) is ParkReason.OTP

    await _move(committing_sessionmaker, queued, ApplicationStatus.RUNNING)
    await _move(committing_sessionmaker, queued, ApplicationStatus.NEEDS_OTP)
    assert await _ring(committing_sessionmaker, queued) is ParkReason.OTP

    async with committing_sessionmaker() as session:
        assert await _notifications(session, queued) == 2


async def test_two_different_parks_still_ring_separately(
    committing_sessionmaker, queued: uuid.UUID
) -> None:
    """What `test_notify.py` already covered, held here against the rewrite.

    `failed` is terminal, so this is also the one park that can never recur.
    """
    await _move(committing_sessionmaker, queued, ApplicationStatus.RUNNING)
    await _move(committing_sessionmaker, queued, ApplicationStatus.NEEDS_REVIEW)
    assert await _ring(committing_sessionmaker, queued) is ParkReason.REVIEW

    await _move(committing_sessionmaker, queued, ApplicationStatus.RUNNING)
    await _move(
        committing_sessionmaker,
        queued,
        ApplicationStatus.FAILED,
        failure_reason=FailureReason.MANUAL_COMPLETION_REQUIRED,
    )
    assert await _ring(committing_sessionmaker, queued) is ParkReason.MANUAL

    async with committing_sessionmaker() as session:
        assert await _notifications(session, queued) == 2


async def test_a_status_reached_without_a_transition_keeps_the_old_check(
    committing_sessionmaker, queued: uuid.UUID
) -> None:
    """No transition event means no boundary, so fall back to whole history.

    Nothing in the pipeline assigns a status directly — §6 requires
    `transition()` — but the fallback must never ring twice for one park, and
    that is the direction worth pinning.
    """
    async with committing_sessionmaker() as session:
        application = await session.get(Application, queued)
        assert application is not None
        application.status = ApplicationStatus.NEEDS_REVIEW.value
        await session.commit()

    assert await _ring(committing_sessionmaker, queued) is ParkReason.REVIEW
    assert await _ring(committing_sessionmaker, queued) is None

    async with committing_sessionmaker() as session:
        assert await _notifications(session, queued) == 1
