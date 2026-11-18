"""Saved links from postings, projects and base résumés to vocabulary skills.

The assistant's graph questions ("which jobs match my projects", "which gaps
recur") were answered by re-reading 100 postings in memory per question, 5-7 s
each. These rows hold the same links for every posting, so a question is a
join. They are derived data: `packages/matching/skill_links.py` writes them
from the source columns and nothing else does, and a posting's links are
re-read whenever its text or the requirements extractor changes.

A link is a fact about its source, not about the owner's ability: a project
"declares" a technology, a posting "requires" it, a résumé "lists" it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
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
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.core.models import Base


class PostingSkill(Base):
    """One posting's reading of one skill, at its strongest (`evidence.skill_evidence`)."""

    __tablename__ = "posting_skills"

    posting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("postings.id", ondelete="CASCADE"), primary_key=True
    )
    #: A `skill_vocab` key.
    skill: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: required, preferred, or named (mentioned under no heading that says which).
    strength: Mapped[str] = mapped_column(String(16), nullable=False)
    #: Offered as one choice among several ("Go or Rust"): not a direct demand.
    is_option: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    #: The posting's line, as the extractor quoted it.
    quote: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))

    __table_args__ = (
        CheckConstraint(
            "strength IN ('required', 'preferred', 'named')", name="ck_posting_skills_strength"
        ),
        Index("ix_posting_skills_skill_strength", "skill", "strength"),
    )


class PostingSkillRead(Base):
    """When a posting's links were written, and from what.

    Without it a posting that asks for nothing and one never read look alike.
    A row is stale when `extractor_version` is not today's or `content_hash`
    is not the posting's.
    """

    __tablename__ = "posting_skill_reads"

    posting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("postings.id", ondelete="CASCADE"), primary_key=True
    )
    extractor_version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The posting's `content_hash` when it was read.
    content_hash: Mapped[str | None] = mapped_column(String(64))
    #: False when the posting had no text to read: unknown, not "asks for nothing".
    readable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ProjectSkill(Base):
    """A technology a project's source declares: its primary language or a dependency file.

    Never its name, description or topics, and never a fork's.
    """

    __tablename__ = "project_skills"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    skill: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: language or dependency.
    origin: Mapped[str] = mapped_column(String(16), primary_key=True)
    #: The name as GitHub or the dependency file gave it.
    declared_as: Mapped[str] = mapped_column(String(200), nullable=False)

    __table_args__ = (
        CheckConstraint("origin IN ('language', 'dependency')", name="ck_project_skills_origin"),
        Index("ix_project_skills_skill", "skill"),
    )


class ResumeSkill(Base):
    """A skill a base résumé names. Tailored résumés are not linked."""

    __tablename__ = "resume_skills"

    resume_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resumes.id", ondelete="CASCADE"), primary_key=True
    )
    skill: Mapped[str] = mapped_column(String(64), primary_key=True)

    __table_args__ = (Index("ix_resume_skills_skill", "skill"),)
