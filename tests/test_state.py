"""State machine tests — CLAUDE.md §6, Gate 0.

The edge-table tests are pure and always run. The tests that assert an
ApplicationEvent lands on every transition need Postgres; they skip when it
isn't reachable (see conftest.py).
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from packages.core.enums import ApplicationStatus, FailureReason
from packages.core.models import Application, ApplicationEvent
from packages.core.state import (
    ALLOWED_TRANSITIONS,
    OWNER_ONLY_TRANSITIONS,
    InvalidTransitionError,
    can_transition,
    is_terminal,
    transition,
)

S = ApplicationStatus


# --------------------------------------------------------------------------
# Pure edge table
# --------------------------------------------------------------------------

LEGAL_EDGES = [
    (S.QUEUED, S.RUNNING),
    (S.RUNNING, S.SUBMITTED),
    (S.RUNNING, S.NEEDS_REVIEW),
    (S.RUNNING, S.NEEDS_OTP),
    (S.RUNNING, S.FAILED),
    (S.NEEDS_REVIEW, S.RUNNING),
    (S.NEEDS_REVIEW, S.FAILED),
    (S.NEEDS_OTP, S.RUNNING),
    # Owner-only. Every supported ATS captchas the apply form and §2.5
    # rules out working around one, so the owner finishes by hand — and
    # that has to be recordable or the pipeline shows a sent application
    # as failed forever. The worker never takes these; see
    # OWNER_ONLY_TRANSITIONS.
    (S.NEEDS_REVIEW, S.SUBMITTED),
    (S.FAILED, S.SUBMITTED),
]


@pytest.mark.parametrize(("frm", "to"), LEGAL_EDGES)
def test_legal_edges_are_permitted(frm: ApplicationStatus, to: ApplicationStatus) -> None:
    assert can_transition(frm, to)


def test_edge_table_contains_exactly_the_documented_edges() -> None:
    actual = {(frm, to) for frm, tos in ALLOWED_TRANSITIONS.items() for to in tos}
    assert actual == set(LEGAL_EDGES)


@pytest.mark.parametrize("status", [S.SUBMITTED, S.FAILED])
def test_the_worker_cannot_leave_a_terminal_status(status: ApplicationStatus) -> None:
    """Terminality means the worker is done, not that no edge exists.

    `claim_work` returns ALREADY_DONE for these, which is what stops a
    redelivered queue task from re-running finished work. `failed` also has
    one outgoing edge — the owner recording an application they completed by
    hand — and that edge is in OWNER_ONLY_TRANSITIONS, which the worker never
    consults. Both properties matter, so this pins the machine-reachable set
    rather than the whole table.
    """
    assert is_terminal(status)

    machine_reachable = {
        to for to in ALLOWED_TRANSITIONS[status] if (status, to) not in OWNER_ONLY_TRANSITIONS
    }

    assert machine_reachable == set()


@pytest.mark.parametrize(
    ("frm", "to"),
    [
        (S.QUEUED, S.SUBMITTED),  # must go through running
        (S.QUEUED, S.NEEDS_REVIEW),
        (S.QUEUED, S.QUEUED),  # self-edges are not transitions
        (S.RUNNING, S.RUNNING),
        (S.NEEDS_OTP, S.SUBMITTED),  # otp resumes work, it does not finish it
        (S.SUBMITTED, S.RUNNING),  # terminal
        (S.FAILED, S.RUNNING),  # the machine does not get to retry it
    ],
)
def test_illegal_edges_are_rejected(frm: ApplicationStatus, to: ApplicationStatus) -> None:
    assert not can_transition(frm, to)


# --------------------------------------------------------------------------
# transition() — needs a database
# --------------------------------------------------------------------------


async def test_transition_records_an_event(db_session, application: Application) -> None:
    await transition(db_session, application, S.RUNNING)
    await db_session.flush()

    assert application.status == S.RUNNING

    events = (
        await db_session.scalars(
            select(ApplicationEvent).where(ApplicationEvent.application_id == application.id)
        )
    ).all()
    assert len(events) == 1
    assert events[0].payload_json == {"from": "queued", "to": "running"}


async def test_every_transition_in_a_run_writes_an_event(
    db_session, application: Application
) -> None:
    """Gate 0: ApplicationEvent rows exist for every transition."""
    path = [
        (S.RUNNING, None),
        (S.NEEDS_REVIEW, None),
        (S.RUNNING, None),
        (S.SUBMITTED, None),
    ]
    for to, reason in path:
        await transition(db_session, application, to, failure_reason=reason)
    await db_session.flush()

    count = await db_session.scalar(
        select(func.count())
        .select_from(ApplicationEvent)
        .where(ApplicationEvent.application_id == application.id)
    )
    assert count == len(path)
    assert application.status == S.SUBMITTED
    assert application.failure_reason is None


async def test_invalid_transition_raises(db_session, application: Application) -> None:
    with pytest.raises(InvalidTransitionError) as exc:
        await transition(db_session, application, S.SUBMITTED)

    assert exc.value.frm is S.QUEUED
    assert exc.value.to is S.SUBMITTED
    assert application.status == S.QUEUED  # unchanged


async def test_invalid_transition_writes_no_event(db_session, application: Application) -> None:
    with pytest.raises(InvalidTransitionError):
        await transition(db_session, application, S.SUBMITTED)
    await db_session.flush()

    count = await db_session.scalar(
        select(func.count())
        .select_from(ApplicationEvent)
        .where(ApplicationEvent.application_id == application.id)
    )
    assert count == 0


async def test_terminal_status_cannot_be_left(db_session, application: Application) -> None:
    await transition(db_session, application, S.RUNNING)
    await transition(db_session, application, S.SUBMITTED)

    with pytest.raises(InvalidTransitionError, match="terminal"):
        await transition(db_session, application, S.RUNNING)


async def test_failing_requires_a_reason(db_session, application: Application) -> None:
    await transition(db_session, application, S.RUNNING)

    with pytest.raises(InvalidTransitionError, match="failure_reason is required"):
        await transition(db_session, application, S.FAILED)


async def test_failure_reason_is_persisted(db_session, application: Application) -> None:
    await transition(db_session, application, S.RUNNING)
    await transition(
        db_session,
        application,
        S.FAILED,
        failure_reason=FailureReason.SITE_ERROR,
    )
    await db_session.flush()

    assert application.status == S.FAILED
    assert application.failure_reason == FailureReason.SITE_ERROR


async def test_failure_reason_rejected_on_non_failure(db_session, application: Application) -> None:
    with pytest.raises(InvalidTransitionError, match="only valid"):
        await transition(
            db_session,
            application,
            S.RUNNING,
            failure_reason=FailureReason.SITE_ERROR,
        )


async def test_rejection_at_review_uses_its_own_reason(
    db_session, application: Application
) -> None:
    await transition(db_session, application, S.RUNNING)
    await transition(db_session, application, S.NEEDS_REVIEW)

    with pytest.raises(InvalidTransitionError, match="rejected_at_review"):
        await transition(
            db_session,
            application,
            S.FAILED,
            failure_reason=FailureReason.JOB_CLOSED,
        )

    await transition(
        db_session,
        application,
        S.FAILED,
        failure_reason=FailureReason.REJECTED_AT_REVIEW,
    )
    assert application.failure_reason == FailureReason.REJECTED_AT_REVIEW


async def test_approval_resumes_a_parked_application(db_session, application: Application) -> None:
    await transition(db_session, application, S.RUNNING)
    await transition(
        db_session,
        application,
        S.NEEDS_REVIEW,
        payload={"question": "Why do you want to work here?"},
    )
    await transition(db_session, application, S.RUNNING)
    await db_session.flush()

    events = (
        await db_session.scalars(
            select(ApplicationEvent)
            .where(ApplicationEvent.application_id == application.id)
            .order_by(ApplicationEvent.at)
        )
    ).all()
    parked = events[1].payload_json
    assert parked is not None
    # The exact question text survives into the audit log — never discarded.
    assert parked["question"] == "Why do you want to work here?"
    assert application.status == S.RUNNING
