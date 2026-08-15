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


class ProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    candidate_id: uuid.UUID
    label: str
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
