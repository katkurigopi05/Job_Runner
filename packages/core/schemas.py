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
    #: How the message was tied to its application: "alias" (exact, from the
    #: +app tag we issued), "inferred" (matched on sender and content), or
    #: "unlinked". An inferred link is a guess and must never read as exact.
    link_method: str = "unlinked"
    link_confidence: float | None = None
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
    closed: bool
    title_similarity: float = 0.0
    body_similarity: float = 0.0
    #: Terms the posting emphasizes that the profile evidences, and does not.
    #: The second list is the actionable one: §2.1 stops the tailorer adding a
    #: skill the résumé lacks, so this is where the owner learns what is
    #: missing and decides whether it is true of them.
    matched_terms: list[str] = Field(default_factory=list)
    missing_terms: list[str] = Field(default_factory=list)
    #: Whether the posting looks real and open — a tier and findings, never a
    #: number, and deliberately not folded into `score`. See
    #: packages/matching/legitimacy.py.
    legitimacy: dict[str, Any] = Field(default_factory=dict)
    #: The score broken into dimensions. Explains the ranking, never produces
    #: it — packages/matching/rubric.py.
    rubric: dict[str, Any] = Field(default_factory=dict)
    #: Hard filters that ruled it out — location, seniority, sponsorship.
    excluded_by: list[str] = Field(default_factory=list)
