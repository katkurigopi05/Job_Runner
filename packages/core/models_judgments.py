"""The owner's grades of what the chat search returns for a question.

`posting_labels` grades a posting against a profile: is this a job for me.
The assistant's search answers a different question, which postings a
question is about, and needs grades of its own: a question, a posting, and
whether that posting is what was asked for. Phase 5 of
`docs/HARNESS_AUDIT_2026-10-15.md` asks for them on a frozen set before any
change to how the search ranks.

A row is written when the pool is built (`packages/matching/judged.py`), with
no grade, and graded by the owner on `/label/questions`. The pool is the
union of what each search mode returned, so a mode is never scored against
postings only another mode could find.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
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


class RetrievalJudgment(Base):
    """One posting the search returned for one frozen question, and its grade."""

    __tablename__ = "retrieval_judgments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    #: Which frozen question set, such as `chat-v1`. A new set is a new
    #: version, never an edit of an old one, so a baseline stays comparable.
    set_version: Mapped[str] = mapped_column(String(32), nullable=False)
    question_key: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The words asked. Stored, so a grade still says what it graded if the
    #: seed file is edited.
    question: Mapped[str] = mapped_column(Text, nullable=False)
    stratum: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Held out from every choice of rule or threshold, and read once.
    held_out: Mapped[bool] = mapped_column(Boolean, nullable=False)
    #: Cascades: a posting deleted takes its grade with it. Retention keeps a
    #: posting with a judgment (`crawler/retention.py`), so that is only ever
    #: the owner's own doing.
    posting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("postings.id", ondelete="CASCADE"), nullable=False
    )
    #: The order the owner is shown a question's postings: a stable shuffle,
    #: so no mode's ranking shows through and leans on the grade.
    pool_order: Mapped[int] = mapped_column(Integer, nullable=False)
    #: `{mode: rank}` for each mode that returned it. Never shown while
    #: grading, for the same reason.
    surfaced_by: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    #: The posting's text when the pool was built, so a later edit of the
    #: posting is visible against the grade.
    content_hash: Mapped[str | None] = mapped_column(String(64))
    #: 0 not what was asked, 1 partly, 2 what was asked for. NULL until
    #: graded. Checked in the database: a grade off the scale breaks the
    #: `2**rel` gain silently rather than raising.
    relevance: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)
    graded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # Building the pool again adds what is new and never a second row,
        # and never touches a grade.
        UniqueConstraint(
            "set_version",
            "question_key",
            "posting_id",
            name="uq_retrieval_judgments_question_posting",
        ),
        CheckConstraint(
            "relevance IS NULL OR relevance BETWEEN 0 AND 2",
            name="ck_retrieval_judgments_relevance_scale",
        ),
        Index("ix_retrieval_judgments_posting", "posting_id"),
        Index("ix_retrieval_judgments_set_question", "set_version", "question_key"),
    )
