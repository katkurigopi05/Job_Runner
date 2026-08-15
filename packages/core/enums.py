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
