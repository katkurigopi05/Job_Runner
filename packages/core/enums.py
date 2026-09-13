"""Enumerations shared across the core package.

These mirror CLAUDE.md §6 exactly. The state machine in `state.py` is the only
place that may move an Application between `ApplicationStatus` values.
"""

from __future__ import annotations

from enum import StrEnum


class ApplicationStatus(StrEnum):
    """Application pipeline status values."""

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
    """Reasons an application failed."""

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
    #: A timed exercise — HackerRank, Codility, a take-home. Ranked above
    #: `info_requested` because it is a real advance in the process, and below
    #: `interview` because it almost always precedes one.
    ASSESSMENT = "assessment"


class Classification(StrEnum):
    """What an inbound message is."""

    INTERVIEW = "interview"
    REJECTION = "rejection"
    OFFER = "offer"
    INFO_REQUEST = "info_request"
    #: An online assessment, coding challenge or take-home.
    #:
    #: Its own class because it is the one inbound message with a *clock* on
    #: it. Filed as `info_request` — where it landed before — it reads as
    #: paperwork; abstaining sends it to `noise` and the owner never sees it.
    #: Either way a 72-hour window expires while the tracker looks calm.
    ASSESSMENT = "assessment"
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
    Classification.ASSESSMENT: Outcome.ASSESSMENT,
    Classification.ACKNOWLEDGEMENT: Outcome.ACKNOWLEDGED,
}


class SourceStatus(StrEnum):
    """How much we actually know about where a company's jobs are listed.

    `Company.careers_url` was one undifferentiated field, so a Google search
    link imported from a spreadsheet was indistinguishable from a Greenhouse
    board we had polled successfully for a month. A dashboard reading that
    column has no way to avoid presenting the first as a career page, which is
    the one thing it must not do.

    These are ordered by how much evidence stands behind them, and nothing
    promotes a row except evidence.
    """

    #: No usable website and no usable careers URL. There is nothing to try,
    #: which is a different problem from having tried and failed — it is fixed
    #: by finding a URL, not by crawling better.
    NO_WEBSITE = "no_website"
    #: A lead: a search-engine link, or the company's home page. Imported as
    #: supplied, never shown as a career page, and the input to discovery.
    HINT = "hint"
    #: A candidate board, not yet confirmed to belong to this company. A slug
    #: guessed from a name lands here — `acme` on Greenhouse may be somebody
    #: else's `acme`.
    UNVERIFIED = "unverified"
    #: The board answered and the evidence ties it to this company.
    VERIFIED = "verified"
    #: Discovery ran and found nothing. Distinct from `no_website` because it
    #: is worth retrying later, and from `unverified` because there is no
    #: candidate to confirm.
    FAILED = "failed"


class EmailMode(StrEnum):
    """Email management mode for candidates."""

    MANAGED = "managed"
    SELF = "self"


class QueueTaskStatus(StrEnum):
    """Queue task status values."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class EventType(StrEnum):
    """Types written to the append-only ApplicationEvent log."""

    CREATED = "created"
    TRANSITION = "transition"
    NOTE = "note"
    #: The owner was told this application is waiting on them. Written after
    #: delivery so a re-run of an at-least-once task does not tell them twice.
    NOTIFIED = "notified"


class ErrorCode(StrEnum):
    """Shared error envelope codes, per CLAUDE.md §10."""

    UNAUTHORIZED = "unauthorized"
    INVALID_REQUEST = "invalid_request"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    DUPLICATE_APPLICATION = "duplicate_application"
    INVALID_STATE = "invalid_state"
    INTERNAL_ERROR = "internal_error"


class SeniorityLevel(StrEnum):
    """The rungs `matching.filters.SENIORITY_LEVELS` knows.

    An enum rather than a free string because a typo here does not raise — it
    silently disables the filter. `filters.seniority_ok` returns True for a
    target it cannot place in the ladder, so `"Senior"` or `"sr"` would read as
    "no preference" and the owner would see an unfiltered feed with nothing to
    explain it.
    """

    INTERN = "intern"
    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    PRINCIPAL = "principal"


class CitizenshipStatus(StrEnum):
    """The owner's *current* authorization, as a fact a filter can act on.

    Separate from `Profile.needs_sponsorship`, which is about the *future*: one
    boolean was deciding both, and they are different questions. A permanent
    resident needs no sponsorship and still fails "US citizens only"; a citizen
    passes both; somebody on OPT may need sponsorship later and be authorized
    now. Collapsing that into one flag is how a posting restricted to citizens
    passed a filter that only knew about sponsorship.

    **For filtering only.** §2.2 makes the answers typed onto an application
    verbatim copies of `Profile.work_auth`, and that is unchanged — nothing
    here is ever written into a form, because a structured guess at a legal
    status is exactly what that rule forbids. This says which postings the owner
    wants to see, which §1 calls the owner's input.

    NULL is the shipped state and means *unstated*: the filter then flags an
    explicit restriction rather than excluding on it, because excluding on a
    field nobody filled in hides real jobs and inferring a status from a résumé
    would be the §1 violation in the other direction.
    """

    US_CITIZEN = "us_citizen"
    PERMANENT_RESIDENT = "permanent_resident"
    #: Authorized to work now by some other means — OPT, TN, H-1B already held.
    #: Deliberately one value: the distinctions between them change what
    #: sponsorship is needed later, not whether a citizens-only posting
    #: excludes the applicant, and this enum answers only the second question.
    OTHER_AUTHORIZED = "other_authorized"
    #: Not currently authorized in the United States.
    NOT_AUTHORIZED = "not_authorized"
