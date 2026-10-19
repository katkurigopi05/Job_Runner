"""Companies a worker could not read: noted, passed over, then looked into.

Asked for by the owner on 2026-10-19. When a worker cannot find a company's
board, or a board it knows stops answering, it notes the company here and
goes on to the next one. Once nothing else is waiting, a deep dive takes each
noted company once and looks harder (`packages/crawler/deep_dive.py`). A
company it fixes is verified and crawled. One it cannot fix stays here as an
error, with what it tried and what a person should do.

One open row per company and stage. A row is kept after it is fixed, so the
history of what went wrong stays readable; a later failure opens a new one.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.core.models import Base

#: Where the worker was when it gave up on the company: finding its board
#: (`discover_company`), or reading a board already found (`crawl_company`).
ISSUE_STAGES = ("discovery", "crawl")

#: `noted` waits for a deep dive, `queued` has one enqueued, `fixed` is done
#: with (by the deep dive or by an ordinary run), and `needs_owner` is the
#: error list: the deep dive found no fix it could make.
ISSUE_STATUSES = ("noted", "queued", "fixed", "needs_owner")

#: The statuses of a row still open.
OPEN_STATUSES = ("noted", "queued", "needs_owner")


class CompanyIssue(Base):
    """One company a worker could not read, and what was found about why."""

    __tablename__ = "company_issues"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    #: What the worker saw, in one word: `SiteReading.category` for discovery,
    #: `crawl_category` for a board.
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    #: The worker's own sentence for it, as last noted.
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="noted")
    times_noted: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    noted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_noted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deep_dived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: What the deep dive tried and what each step found.
    findings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    #: The deep dive's finding in one sentence, and what a person should do.
    diagnosis: Mapped[str | None] = mapped_column(Text)
    fix: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: `deep_dive`, `discovery` or `crawl`: what closed it.
    resolved_by: Mapped[str | None] = mapped_column(String(16))

    __table_args__ = (
        CheckConstraint(
            "stage IN ('discovery', 'crawl')",
            name="ck_company_issues_stage",
        ),
        CheckConstraint(
            "status IN ('noted', 'queued', 'fixed', 'needs_owner')",
            name="ck_company_issues_status",
        ),
        # One open issue per company and stage. A fixed row stays as history.
        Index(
            "uq_company_issues_open",
            "company_id",
            "stage",
            unique=True,
            postgresql_where=text("status <> 'fixed'"),
        ),
        Index("ix_company_issues_status_noted", "status", "noted_at"),
    )
