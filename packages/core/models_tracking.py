"""Contacts and tasks attached to applications.

The tracker knew an application had reached `interview` or `assessment` —
inbox routing records that — but not who the recruiter was, when the call is,
or what is left to prepare. Those lived in the owner's head or a separate
notes app, which is where deadlines are missed.
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
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.core.models import Base

TASK_KINDS = ("interview", "assessment", "follow_up", "prep", "other")
RELATIONSHIPS = ("recruiter", "hiring_manager", "interviewer", "referrer", "other")


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )


def _stamp() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Contact(Base):
    """A person the owner is in touch with about a job. Their details, the owner's notes."""

    __tablename__ = "contacts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(50))
    company: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[str | None] = mapped_column(String(200))
    profile_url: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _stamp()
    updated_at: Mapped[datetime] = _stamp()

    __table_args__ = (Index("ix_contacts_candidate_id", "candidate_id"),)


class ApplicationContact(Base):
    """Who this person is to this application."""

    __tablename__ = "application_contacts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    relationship: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default=text("'recruiter'")
    )
    created_at: Mapped[datetime] = _stamp()

    __table_args__ = (
        UniqueConstraint("application_id", "contact_id", name="uq_application_contacts_pair"),
        CheckConstraint(
            "relationship IN ('recruiter', 'hiring_manager', 'interviewer', 'referrer', 'other')",
            name="ck_application_contacts_relationship",
        ),
    )


class ApplicationTask(Base):
    """An interview, an assessment, a follow-up to write — with a deadline and a checklist."""

    __tablename__ = "application_tasks"

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: When to ring the local reminder. Never after `due_at`.
    reminder_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Set once the reminder rang, so a restarted worker does not ring it again.
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: A meeting link or an address.
    location: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    #: `[{"text": ..., "done": bool}]`.
    checklist_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    #: `owner`, or `inbox` when created from a routed interview/assessment reply.
    source: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'owner'"))
    created_at: Mapped[datetime] = _stamp()
    updated_at: Mapped[datetime] = _stamp()

    __table_args__ = (
        CheckConstraint(
            "kind IN ('interview', 'assessment', 'follow_up', 'prep', 'other')",
            name="ck_application_tasks_kind",
        ),
        CheckConstraint("source IN ('owner', 'inbox')", name="ck_application_tasks_source"),
        CheckConstraint(
            "reminder_at IS NULL OR due_at IS NULL OR reminder_at <= due_at",
            name="ck_application_tasks_reminder_before_due",
        ),
        Index("ix_application_tasks_application_id", "application_id"),
        Index("ix_application_tasks_due_at", "due_at"),
        Index("ix_application_tasks_reminder_at", "reminder_at"),
    )


__all__ = [
    "RELATIONSHIPS",
    "TASK_KINDS",
    "ApplicationContact",
    "ApplicationTask",
    "Contact",
]
