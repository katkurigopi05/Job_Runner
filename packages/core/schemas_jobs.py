"""API shapes for a posting's sources and version history."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class VersionChangeOut(BaseModel):
    field: str
    before: Any = None
    after: Any = None


class PostingVersionOut(BaseModel):
    version: int
    captured_at: datetime
    pay: str
    #: Changes from the previous version. Empty for the first.
    changes: list[VersionChangeOut] = Field(default_factory=list)


class PostingSourceOut(BaseModel):
    """One listing of the requisition — every one kept, none hidden."""

    posting_id: uuid.UUID
    url: str
    ats_type: str | None
    source: str
    title: str | None
    location: str | None
    closed: bool
    #: Match decisions recorded on this listing, by any profile.
    decisions: list[str] = Field(default_factory=list)
    #: Statuses of applications made through this listing.
    application_statuses: list[str] = Field(default_factory=list)
    #: Why the grouping attached it, when it was attached rather than the first.
    evidence: str | None = None


class PostingHistoryOut(BaseModel):
    posting_id: uuid.UUID
    title: str | None
    canonical_job_id: uuid.UUID | None
    #: The owner split this listing out; it will not be grouped again.
    locked: bool
    sources: list[PostingSourceOut]
    versions: list[PostingVersionOut]


__all__ = ["PostingHistoryOut", "PostingSourceOut", "PostingVersionOut", "VersionChangeOut"]
