"""Interview prep API and persisted record formats."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints

Short = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
Topic = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
SourceKind = Literal[
    "recruiter-confirmed",
    "posting",
    "resume",
    "project",
    "internship",
    "coursework",
    "official employer guidance",
    "firsthand report",
    "unverified public source",
    "round notes",
]


class Details(BaseModel):
    round: Short = ""
    date: dt.date | None = None
    time: dt.time | None = None
    timezone: Short = ""
    format: Short = ""
    duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    topics: list[Topic] = Field(default_factory=list, max_length=10)


class Confirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    round_id: UUID | None = None
    invitation_id: UUID | None = None
    recruiter_note: str = Field(default="", max_length=2500)
    details: Details
    resume_id: UUID | None = None
    resume_unknown: bool = False
    description: str | None = Field(default=None, max_length=32000)
    prep_minutes: int = Field(default=60, ge=5, le=6000)
    research_urls: list[HttpUrl] = Field(default_factory=list, max_length=6)
    research: bool = False
    share_mail: bool = False


class Evidence(BaseModel):
    id: str
    kind: SourceKind
    reference: str
    excerpt: str
    project_id: UUID | None = None
    summary: str = ""
    superseded: bool = False
    url: str | None = None
    published: dt.date | None = None
    retrieved: str | None = None
    role: str | None = None
    level: str | None = None
    location: str | None = None
    round: str | None = None
    caveat: str = ""


class Requirement(BaseModel):
    id: str
    topic: str
    wording: str
    skills: list[str] = Field(default_factory=list)


class Coverage(BaseModel):
    requirement: Requirement
    status: Literal["demonstrated", "partial", "missing"]
    evidence_ids: list[str]
    next_action: str


class TopicPlan(BaseModel):
    topic: str
    why: str
    evidence_ids: list[str]


class PracticeBlock(BaseModel):
    minutes: int
    exercise: str


class Plan(BaseModel):
    topics: list[TopicPlan]
    next_hour: list[PracticeBlock]
    later: list[PracticeBlock]
    coverage: list[Coverage]
    questions: list[str]
    followups: list[str]
    ask: list[str]
    model_questions: list[str] = Field(default_factory=list)
    model_followups: list[str] = Field(default_factory=list)
    model_ask: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


class Snapshot(BaseModel):
    company: str = ""
    company_domain: str = ""
    role: str = ""
    location: str = ""
    details: Details
    posting_ref: str
    posting_description: str
    posting_hash: str
    posting_version: int | None = None
    resume_ref: str
    resume_id: UUID | None = None
    resume_version: int | None = None
    invitation_id: UUID | None = None
    recruiter_excerpt: str = ""
    prep_minutes: int
    urls: list[str]
    research: bool
    share_mail: bool
    fixture: bool = False
