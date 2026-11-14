"""What the owner states about themselves beyond the résumé: internships and certifications.

For the Resume Studio (`packages/tailor/studio.py`). These rows are the
owner's own statements, typed on the dashboard and confirmed there
(`verified`). Nothing here is written by a model, and no MCP tool writes
either table: a record is what lets a certification or an internship appear
on a résumé, so only the owner makes one (CLAUDE.md §2.1).

Dates are kept as the owner writes them ("Jun 2025"), because they are
printed as written and never computed with.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.core.models import Base

_EMPTY_LIST = text("'[]'::jsonb")


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )


def _candidate() -> Mapped[uuid.UUID]:
    return mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Internship(Base):
    """One internship, as the owner states it."""

    __tablename__ = "internships"

    id: Mapped[uuid.UUID] = _pk()
    candidate_id: Mapped[uuid.UUID] = _candidate()
    company: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(200), nullable=False)
    location: Mapped[str | None] = mapped_column(String(200))
    started: Mapped[str] = mapped_column(String(40), nullable=False)
    ended: Mapped[str | None] = mapped_column(String(40))
    #: Tools used there. Evidence for this internship's own bullets and no
    #: other entry's (`guard.SourceCorpus.from_resume`, `verified`).
    stack: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_LIST)
    #: What was done, in the owner's words, figures included.
    bullets: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_LIST)
    #: The owner confirmed the record. Only a confirmed one reaches a résumé.
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (Index("ix_internships_candidate", "candidate_id"),)


class Certification(Base):
    """One certification, as the owner states it."""

    __tablename__ = "certifications"

    id: Mapped[uuid.UUID] = _pk()
    candidate_id: Mapped[uuid.UUID] = _candidate()
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    issuer: Mapped[str] = mapped_column(String(200), nullable=False)
    issued: Mapped[str | None] = mapped_column(String(40))
    expires: Mapped[str | None] = mapped_column(String(40))
    credential_id: Mapped[str | None] = mapped_column(String(200))
    #: Skills the certification covers. Printed in the Skills list by code;
    #: never a reason to rewrite an employer's or a project's bullet.
    skills: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_LIST)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (Index("ix_certifications_candidate", "candidate_id"),)
