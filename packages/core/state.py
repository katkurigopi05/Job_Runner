"""Application state machine — CLAUDE.md §6.

`transition()` is the *only* place an Application's status may change. It
validates the edge, writes an append-only ApplicationEvent, and mutates the
row. Direct assignment to `Application.status` anywhere else is a bug.

    queued ──> running ──> submitted        (terminal, success)
                 │  ▲
                 │  ├── needs_review ──approve──> running
                 │  │                └──reject──> failed[rejected_at_review]
                 │  └── needs_otp ────otp────────> running
                 └──────────────────────────────> failed  (terminal)
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.enums import (
    TERMINAL_STATUSES,
    ApplicationStatus,
    ErrorCode,
    EventType,
    FailureReason,
)
from packages.core.models import Application, ApplicationEvent

#: The complete edge set. Anything not listed here is invalid by construction.
ALLOWED_TRANSITIONS: dict[ApplicationStatus, frozenset[ApplicationStatus]] = {
    ApplicationStatus.QUEUED: frozenset({ApplicationStatus.RUNNING}),
    ApplicationStatus.RUNNING: frozenset(
        {
            ApplicationStatus.SUBMITTED,
            ApplicationStatus.NEEDS_REVIEW,
            ApplicationStatus.NEEDS_OTP,
            ApplicationStatus.FAILED,
        }
    ),
    ApplicationStatus.NEEDS_REVIEW: frozenset(
        {ApplicationStatus.RUNNING, ApplicationStatus.FAILED, ApplicationStatus.SUBMITTED}
    ),
    ApplicationStatus.NEEDS_OTP: frozenset({ApplicationStatus.RUNNING}),
    ApplicationStatus.SUBMITTED: frozenset(),
    # `failed` is where the *machine* gives up, not where the application
    # ends. Every supported ATS mounts a captcha on the apply form and §2.5
    # rules out working around one, so the common failure is
    # `manual_completion_required` — an application the owner then finishes by
    # hand. Without this edge that work could never be recorded, and the
    # pipeline board would show a submitted application as failed forever.
    ApplicationStatus.FAILED: frozenset({ApplicationStatus.SUBMITTED}),
}

#: Edges only an explicit owner action may take. The worker drives
#: queued -> running -> {submitted, needs_review, needs_otp, failed} and never
#: touches these, so a redelivered task still cannot resurrect a terminal row —
#: which is the property that made `submitted` and `failed` terminal in the
#: first place. A person saying "I sent this myself" is a different thing from
#: a retry.
OWNER_ONLY_TRANSITIONS: frozenset[tuple[ApplicationStatus, ApplicationStatus]] = frozenset(
    {
        (ApplicationStatus.NEEDS_REVIEW, ApplicationStatus.SUBMITTED),
        (ApplicationStatus.FAILED, ApplicationStatus.SUBMITTED),
    }
)


class InvalidTransitionError(Exception):
    """Raised when a caller attempts an edge the machine does not permit.

    Surfaces to the API as the `invalid_state` error code.
    """

    code = ErrorCode.INVALID_STATE

    def __init__(
        self,
        frm: ApplicationStatus,
        to: ApplicationStatus,
        detail: str | None = None,
    ) -> None:
        self.frm = frm
        self.to = to
        message = f"invalid transition {frm} -> {to}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


def is_terminal(status: ApplicationStatus) -> bool:
    """Whether the *worker* is finished with this application.

    Deliberately not "has no outgoing edges" any more, and the difference is
    load-bearing in both directions:

    - `claim_work` returns ALREADY_DONE for a terminal status, which is what
      stops a redelivered queue task from re-running finished work. That must
      keep covering `failed`.
    - `failed -> submitted` exists so the owner can record an application they
      completed by hand after a captcha stopped the machine (§2.5).

    So `failed` is terminal *and* has an outgoing edge, and both are correct
    because they answer different questions. The edge is listed in
    `OWNER_ONLY_TRANSITIONS`, which the worker never consults. Do not "fix"
    this by emptying one to match the other: emptying the edge makes
    hand-finished applications unrecordable, and clearing the terminal flag
    lets the queue resurrect them.
    """
    return status in TERMINAL_STATUSES


def can_transition(frm: ApplicationStatus, to: ApplicationStatus) -> bool:
    """Pure edge check. Callers guard with this to stay idempotent.

    Queue delivery is at-least-once, so a handler replayed after a committed
    status change must check before calling `transition()` rather than
    assuming the row is still where it left it.
    """
    return to in ALLOWED_TRANSITIONS[frm]


def _validate(
    frm: ApplicationStatus,
    to: ApplicationStatus,
    failure_reason: FailureReason | None,
) -> None:
    if not can_transition(frm, to):
        detail = "status is terminal" if is_terminal(frm) else None
        raise InvalidTransitionError(frm, to, detail)

    if to is ApplicationStatus.FAILED:
        if failure_reason is None:
            raise InvalidTransitionError(frm, to, "failure_reason is required")
        # The diagram admits exactly one way to fail out of review: rejection.
        if (
            frm is ApplicationStatus.NEEDS_REVIEW
            and failure_reason is not FailureReason.REJECTED_AT_REVIEW
        ):
            raise InvalidTransitionError(
                frm, to, "failing out of needs_review must be rejected_at_review"
            )
    elif failure_reason is not None:
        raise InvalidTransitionError(
            frm, to, "failure_reason is only valid when transitioning to failed"
        )


async def transition(
    session: AsyncSession,
    application: Application,
    to: ApplicationStatus,
    *,
    failure_reason: FailureReason | None = None,
    payload: dict[str, Any] | None = None,
) -> Application:
    """Move `application` to `to`, recording an event. Does not commit.

    The caller owns the transaction so that a status change and whatever
    prompted it land atomically.

    Raises:
        InvalidTransitionError: the edge is not permitted, or `failure_reason`
            is missing or inconsistent with the target status.
    """
    frm = ApplicationStatus(application.status)
    _validate(frm, to, failure_reason)

    application.status = to.value
    application.failure_reason = failure_reason.value if failure_reason else None

    event_payload: dict[str, Any] = {"from": frm.value, "to": to.value}
    if failure_reason is not None:
        event_payload["failure_reason"] = failure_reason.value
    if payload:
        event_payload.update(payload)

    session.add(
        ApplicationEvent(
            application_id=application.id,
            type=EventType.TRANSITION.value,
            payload_json=event_payload,
        )
    )
    return application


class WorkClaim(StrEnum):
    """What `begin_work()` found when a worker picked the application up."""

    #: Moved into running from a resting state.
    STARTED = "started"
    #: Already running — a previous attempt died mid-flight. Resume it.
    RESUMED = "resumed"
    #: Terminal already. The work is done; ack the task and move on.
    ALREADY_DONE = "already_done"


async def begin_work(
    session: AsyncSession,
    application: Application,
    *,
    payload: dict[str, Any] | None = None,
) -> WorkClaim:
    """Put an application into `running` for a worker that holds its task lease.

    This exists because `can_transition()` alone deadlocks a retried task. If a
    worker commits `queued -> running` and then dies, the redelivered task finds
    the row already `running`, and `running -> running` is not a legal edge —
    so a handler that only guards with `can_transition()` can neither proceed
    nor legally fail.

    The queue lease resolves it. The caller only reaches this function while
    holding an unexpired lease on the task, which means no other worker can be
    acting on it, which means an already-`running` row is an abandoned attempt
    and is safe to resume. Resuming writes no transition event because no edge
    was traversed; the queue's `attempts` counter is the record of the retry.

    Callers MUST hold the task lease. Calling this without one races.
    """
    status = ApplicationStatus(application.status)

    if is_terminal(status):
        return WorkClaim.ALREADY_DONE

    if status is ApplicationStatus.RUNNING:
        return WorkClaim.RESUMED

    await transition(session, application, ApplicationStatus.RUNNING, payload=payload)
    return WorkClaim.STARTED
