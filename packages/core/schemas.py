"""Pydantic models for every API boundary. No bare dicts cross a module edge."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from packages.core.enums import ApplicationStatus, EmailMode, FailureReason


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    """The shared error envelope, CLAUDE.md §10."""

    error: ErrorDetail


# --------------------------------------------------------------------------
# Candidates
# --------------------------------------------------------------------------


class CandidateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    email_mode: EmailMode = EmailMode.SELF
    managed_alias: str | None = None


class CandidateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: str
    email_mode: str
    managed_alias: str | None
    created_at: datetime


# --------------------------------------------------------------------------
# Profiles
# --------------------------------------------------------------------------


class ProfileCreate(BaseModel):
    candidate_id: uuid.UUID
    label: str = Field(min_length=1, max_length=200)
    phone: str | None = None
    location: str | None = None
    #: Copied verbatim onto applications — never LLM-generated. CLAUDE.md §2.2.
    work_auth: str | None = None
    needs_sponsorship: bool | None = None
    links: dict[str, str] = Field(default_factory=dict)
    salary_expectation: str | None = None
    answers: dict[str, Any] = Field(default_factory=dict)
    min_match_score: float = Field(default=0.75, ge=0.0, le=1.0)
    auto_submit: bool = False


class ProfileUpdate(BaseModel):
    """A partial edit. Every field is optional and `None` means "not supplied".

    Deliberately not reusing ProfileCreate: there, an omitted work_auth means
    "no answer yet", while here it has to mean "leave it as it is". Sharing one
    model would make a profile edit silently blank the answers §2.2 requires be
    copied verbatim onto real applications.
    """

    label: str | None = Field(default=None, min_length=1, max_length=200)
    phone: str | None = None
    location: str | None = None
    work_auth: str | None = None
    needs_sponsorship: bool | None = None
    links: dict[str, str] | None = None
    salary_expectation: str | None = None
    answers: dict[str, Any] | None = None
    min_match_score: float | None = Field(default=None, ge=0.0, le=1.0)
    auto_submit: bool | None = None


class ProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    candidate_id: uuid.UUID
    label: str
    #: The résumé this profile applies with. Exposed so a caller can show the
    #: document before it is sent rather than naming a file it cannot read.
    base_resume_id: uuid.UUID | None
    phone: str | None
    location: str | None
    work_auth: str | None
    needs_sponsorship: bool | None
    salary_expectation: str | None
    min_match_score: float
    auto_submit: bool
    created_at: datetime


# --------------------------------------------------------------------------
# Applications
# --------------------------------------------------------------------------


class ApplicationCreate(BaseModel):
    candidate_id: uuid.UUID
    profile_id: uuid.UUID
    url: str = Field(min_length=1)
    ats: str | None = None


class ApplicationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    candidate_id: uuid.UUID
    profile_id: uuid.UUID
    url: str
    ats: str | None
    status: ApplicationStatus
    failure_reason: FailureReason | None
    review: dict[str, Any] | None = Field(default=None, validation_alias="review_json")
    #: The tailored résumé this application will attach, once one exists. Null
    #: means the profile's base résumé goes as-is. Exposed so the review screen
    #: can show the actual document before it is sent, rather than asking the
    #: owner to approve a file they cannot see.
    tailored_resume_id: uuid.UUID | None = None
    #: What the employer's reply said, once one arrived — interview, rejection,
    #: offer. Distinct from `status`, which tracks *our* side of the work: an
    #: application is `submitted` the moment it is sent and stays there whether
    #: the answer is an offer or silence.
    outcome: str | None = None
    outcome_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ReviewDecision(BaseModel):
    """Approve or reject a parked application. CLAUDE.md §2.3 — this is the
    gate that stands between a filled form and a real submission."""

    approve: bool
    #: Answers the owner supplied for questions the agent could not fill.
    answers: dict[str, Any] = Field(default_factory=dict)
    note: str | None = None


class OtpSubmission(BaseModel):
    """A verification code the site asked for.

    Short-lived and single-use. It is handed to the worker through the
    application row and cleared once consumed; it is never logged.
    """

    code: str = Field(min_length=1, max_length=32)


class ApplicationEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: str
    payload: dict[str, Any] | None = Field(default=None, validation_alias="payload_json")
    at: datetime


# --------------------------------------------------------------------------
# Projects
# --------------------------------------------------------------------------


class SyncGitHubRequest(BaseModel):
    candidate_id: uuid.UUID
    username: str = Field(min_length=1, max_length=100)
    #: Overrides GITHUB_TOKEN for this call. Never stored, never logged.
    token: str | None = None
    include_private: bool = False


class SyncResultOut(BaseModel):
    added: int
    updated: int
    total: int


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source: str
    name: str
    full_name: str | None
    url: str
    description: str | None
    language: str | None
    topics: list[str] = Field(default_factory=list, validation_alias="topics_json")
    stars: int
    is_fork: bool
    is_archived: bool
    is_private: bool
    pushed_at: datetime | None
    include: bool | None
    pinned: bool


class ProjectUpdate(BaseModel):
    """Owner curation. A sync never overwrites these."""

    include: bool | None = None
    pinned: bool | None = None


class ProjectPreview(BaseModel):
    """What would appear on a résumé, and why it ranked where it did."""

    id: uuid.UUID
    name: str
    description: str | None
    url: str
    score: float
    pinned: bool
    #: Exactly the text that will be rendered as the link.
    rendered_link: str


# --------------------------------------------------------------------------
# Résumés
# --------------------------------------------------------------------------


class ResumeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    candidate_id: uuid.UUID
    version: int
    storage_ref: str
    is_default: bool
    created_at: datetime


class ResumeParsedOut(BaseModel):
    """What the parser extracted, so it can be checked before it is trusted."""

    id: uuid.UUID
    version: int
    contact: dict[str, Any]
    #: Section name → line count. A missing section here is a warning sign.
    sections: dict[str, int]
    line_count: int
    parsed: dict[str, Any]


# --------------------------------------------------------------------------
# Postings
# --------------------------------------------------------------------------


class PostingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    url: str
    title: str | None
    location: str | None
    ats_type: str | None
    external_id: str | None
    #: When the source says it went up. None when the board does not say.
    published_at: datetime | None = None
    first_seen_at: datetime
    closed_at: datetime | None


class PostingSearchOut(BaseModel):
    results: list[PostingOut] = Field(default_factory=list)
    total_indexed: int = 0
    #: Set when the empty result means "nothing indexed" rather than "no match".
    note: str | None = None


class ResumePreviewOut(BaseModel):
    """What an assembled résumé would contain, without rendering it."""

    resume_id: uuid.UUID
    version: int
    sections: list[str] = Field(default_factory=list)
    project_names: list[str] = Field(default_factory=list)
    source_line_count: int = 0
    #: Exactly the text each project link will render as.
    rendered_links: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Inbox
# --------------------------------------------------------------------------


class InboundMessageOut(BaseModel):
    """A recruiter reply, as it was received.

    Subject and body are the sender's words, kept verbatim: the classification
    is a guess and the owner needs the original to check it against. They are
    also somebody else's personal correspondence, so they stay on this machine
    — CLAUDE.md §2.8.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    candidate_id: uuid.UUID
    application_id: uuid.UUID | None
    from_addr: str
    subject: str | None
    body: str | None
    classification: str | None
    at: datetime


# --------------------------------------------------------------------------
# Assistant
# --------------------------------------------------------------------------


class ChatRequest(BaseModel):
    """A question about the owner's own job search."""

    message: str = Field(min_length=1, max_length=4000)
    #: Scopes the answer to one application, so "what is it waiting on?" works.
    application_id: uuid.UUID | None = None


class ChatReply(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    reply: str
    #: Which provider answered, or "refused" when a §2.2 boundary stopped it.
    #: Surfaced so the owner can see the assistant really is running locally.
    provider: str
    #: False when the answer came from a rule rather than the model.
    grounded: bool


# --------------------------------------------------------------------------
# Matches
# --------------------------------------------------------------------------


class MatchOut(BaseModel):
    """A scored posting, with the breakdown that produced the score.

    The reasoning travels with the number on purpose. A feed that shows a score
    and not why is a ranking the owner has to take on trust, and this score
    decides what gets applied to.
    """

    id: uuid.UUID
    profile_id: uuid.UUID
    posting_id: uuid.UUID
    score: float
    title: str | None
    location: str | None
    url: str
    ats_type: str | None
    first_seen_at: datetime
    published_at: datetime | None = None
    #: Hours between the source publishing and the crawler seeing it. None when
    #: the board reports no date — an unmeasurable lag, not a lag of zero.
    lag_hours: float | None = None
    closed: bool
    title_similarity: float = 0.0
    body_similarity: float = 0.0
    #: Hard filters that ruled it out — location, seniority, sponsorship.
    excluded_by: list[str] = Field(default_factory=list)


class PacketPosting(BaseModel):
    """What the owner needs to see to recognize the job they are applying to."""

    title: str | None = None
    company: str | None = None
    location: str | None = None
    url: str | None = None
    #: Truncated. The full text is on the posting record; this is for reading
    #: on the handoff screen, not for archival.
    description: str | None = None


class PacketResume(BaseModel):
    """The document to upload, and whether tailoring actually produced it."""

    resume_id: uuid.UUID
    download_path: str
    #: False means this is the profile's base résumé — tailoring produced
    #: nothing, and the owner should know that rather than assume otherwise.
    is_tailored: bool
    rewritten_bullets: int = 0
    rejected_rewrites: int = 0


class PacketAnswer(BaseModel):
    """One field we filled, in the employer's own wording."""

    question: str
    value: str


class PacketQuestion(BaseModel):
    """One field we could not fill, in the employer's exact wording (§2.4)."""

    question: str
    kind: str | None = None
    required: bool = False


class ApplicationPacketOut(BaseModel):
    """Everything needed to finish one application by hand.

    This exists because of where the pipeline actually stops. Every ATS we
    support mounts a captcha at the fill stage, and §2.5 forbids working
    around one, so the last step belongs to the owner. That is only a
    reasonable ask if the handoff is complete: the posting to confirm, the
    file to upload, the answers to copy, and the questions nobody could
    answer. Sending someone back to a bare URL wastes everything the run did.
    """

    application_id: uuid.UUID
    status: ApplicationStatus
    failure_reason: FailureReason | None = None
    ats: str | None = None
    apply_url: str
    posting: PacketPosting | None = None
    resume: PacketResume | None = None
    answers: list[PacketAnswer] = Field(default_factory=list)
    unanswered: list[PacketQuestion] = Field(default_factory=list)
    #: Path to the screenshot of the form as we left it filled.
    screenshot_path: str | None = None
    #: True when the form was filled and only submission remains.
    ready_to_submit: bool = False


class FunnelOut(BaseModel):
    """Where applications stop, and whether the score predicts anything."""

    total: int = 0
    submitted: int = 0
    needs_review: int = 0
    failed: int = 0
    answered: int = 0
    engaged: int = 0
    #: Null rather than 0.0 when nothing has been submitted. An empty
    #: denominator is unknown, and 0% reads as "every employer ignored you".
    answer_rate: float | None = None
    engagement_rate: float | None = None
    unscored: int = 0
    #: Null until at least two score bands hold enough applications to read a
    #: rate from — which is the honest answer for most of this tool's life.
    score_tracks_outcome: bool | None = None
    buckets: list[dict[str, Any]] = Field(default_factory=list)


class LatencyOut(BaseModel):
    """How long employers took to answer, from the owner's own history."""

    samples: int = 0
    median_days: float | None = None
    fastest_days: int | None = None
    slowest_days: int | None = None
    #: Split because a rejection and an interview invitation travel at very
    #: different speeds, and averaging them describes neither.
    median_rejection_days: float | None = None
    median_engagement_days: float | None = None
    suggested_silent_after_days: int | None = None


class SilentOut(BaseModel):
    application_id: str
    url: str
    days_since: int
    stale: bool


class CadenceOut(BaseModel):
    silent: list[SilentOut] = Field(default_factory=list)
    due: int = 0
    stale: int = 0
    latency: LatencyOut = Field(default_factory=LatencyOut)


class DigestOut(BaseModel):
    window_days: int = 7
    postings_seen: int = 0
    applications_created: int = 0
    applications_submitted: int = 0
    replies_received: int = 0
    awaiting_review: int = 0
    follow_ups_due: int = 0
    #: Named rather than left as six zeroes: a quiet week usually means the
    #: crawler stopped, not that the market did.
    quiet_week: bool = False
    funnel: FunnelOut = Field(default_factory=FunnelOut)
    latency: LatencyOut = Field(default_factory=LatencyOut)
