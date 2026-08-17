"""Enumerations shared across the core package.

These mirror CLAUDE.md §6 exactly. The state machine in `state.py` is the only
place that may move an Application between `ApplicationStatus` values.
"""

from __future__ import annotations

from enum import StrEnum


class ApplicationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    NEEDS_REVIEW = "needs_review"
    NEEDS_OTP = "needs_otp"
    SUBMITTED = "submitted"
    FAILED = "failed"


#: Statuses from which no further transition is legal.
TERMINAL_STATUSES: frozenset[ApplicationStatus] = frozenset(
    {ApplicationStatus.SUBMITTED, ApplicationStatus.FAILED}
)


class FailureReason(StrEnum):
    JOB_CLOSED = "job_closed"
    UNSUPPORTED_SITE = "unsupported_site"
    INCOMPLETE_CANDIDATE = "incomplete_candidate"
    MANUAL_COMPLETION_REQUIRED = "manual_completion_required"
    REJECTED_AT_REVIEW = "rejected_at_review"
    SITE_ERROR = "site_error"


class Outcome(StrEnum):
    """What the employer did — a different axis from ApplicationStatus.

    `status` tracks *our automation*: queued, running, submitted, failed. It
    ends at `submitted`, and that terminality is what makes the queue safe to
    retry. What happens afterwards is the employer's business, not ours, so a
    rejection email records an outcome instead of trying to move a terminal
    status. CLAUDE.md §6 stays exactly as written.
    """

    #: Submitted, nothing heard back yet.
    AWAITING = "awaiting"
    ACKNOWLEDGED = "acknowledged"
    INTERVIEW = "interview"
    OFFER = "offer"
    REJECTED = "rejected"
    #: They asked the applicant for something.
    INFO_REQUESTED = "info_requested"


class Classification(StrEnum):
    """What an inbound message is."""

    INTERVIEW = "interview"
    REJECTION = "rejection"
    OFFER = "offer"
    INFO_REQUEST = "info_request"
    ACKNOWLEDGEMENT = "acknowledgement"
    #: A verification code — the one kind that legitimately moves status.
    OTP = "otp"
    NOISE = "noise"


#: How a classification maps onto an outcome. NOISE and OTP deliberately map to
#: nothing: neither says anything about the employer's decision.
OUTCOME_FOR_CLASSIFICATION: dict[Classification, Outcome] = {
    Classification.INTERVIEW: Outcome.INTERVIEW,
    Classification.REJECTION: Outcome.REJECTED,
    Classification.OFFER: Outcome.OFFER,
    Classification.INFO_REQUEST: Outcome.INFO_REQUESTED,
    Classification.ACKNOWLEDGEMENT: Outcome.ACKNOWLEDGED,
}


class EmailMode(StrEnum):
    MANAGED = "managed"
    SELF = "self"


class QueueTaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class EventType(StrEnum):
    """Types written to the append-only ApplicationEvent log."""

    CREATED = "created"
    TRANSITION = "transition"
    NOTE = "note"


class ErrorCode(StrEnum):
    """Shared error envelope codes, per CLAUDE.md §10."""

    UNAUTHORIZED = "unauthorized"
    INVALID_REQUEST = "invalid_request"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    DUPLICATE_APPLICATION = "duplicate_application"
    INVALID_STATE = "invalid_state"
    INTERNAL_ERROR = "internal_error"
