"""Canonical jobs and posting versions.

`Posting` is one row per (company, external id): one listing on one source.
Nothing said that the Greenhouse listing and the careers-page JSON-LD listing
were the same requisition, and nothing kept what a listing said before an
edit — the upsert overwrote it, so "the salary went down" was unanswerable.

- `CanonicalJob` groups listings confidently identified as one requisition.
  Every listing keeps its own row, URL, matches and applications; the group
  is only ever an extra link. See `matching/canonical.py` for what counts as
  confident and what is refused.
- `PostingVersion` is an append-only snapshot of the fields a person compares:
  title, location, pay, and the structured requirements. Never the full text,
  which the posting row already holds once.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.core.models import Base


class CanonicalJob(Base):
    """One requisition, listed by one or more postings."""

    __tablename__ = "canonical_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    #: `titles.canonical(title)` — the equality form the merge compared.
    title_key: Mapped[str] = mapped_column(String(300), nullable=False)
    requisition_id: Mapped[str | None] = mapped_column(String(64))
    #: Why each member was attached: the rule and its confidence, per posting.
    evidence_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_canonical_jobs_company_title", "company_id", "title_key"),)


class PostingVersion(Base):
    """What one posting said, as of one change."""

    __tablename__ = "posting_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    posting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("postings.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    salary_min: Mapped[float | None] = mapped_column(Float)
    salary_max: Mapped[float | None] = mapped_column(Float)
    salary_currency: Mapped[str | None] = mapped_column(String(3))
    salary_period: Mapped[str | None] = mapped_column(String(10))
    requirements_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        UniqueConstraint("posting_id", "version", name="uq_posting_versions_posting_version"),
        Index("ix_posting_versions_posting_id", "posting_id"),
    )


__all__ = ["CanonicalJob", "PostingVersion"]
