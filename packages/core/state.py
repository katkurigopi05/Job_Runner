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
        {ApplicationStatus.RUNNING, ApplicationStatus.FAILED}
    ),
    ApplicationStatus.NEEDS_OTP: frozenset({ApplicationStatus.RUNNING}),
    ApplicationStatus.SUBMITTED: frozenset(),
    ApplicationStatus.FAILED: frozenset(),
}


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
