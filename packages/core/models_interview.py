"""Durable interview rounds and immutable preparation inputs."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.core.models import Base, _pk
from packages.core.models_chunks import LARGE_EMBEDDING_DIM


class InterviewRound(Base):
    __tablename__ = "interview_rounds"

    id: Mapped[uuid.UUID] = _pk()
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("application_tasks.id", ondelete="SET NULL"), unique=True
    )
    notes: Mapped[str | None] = mapped_column(String(6000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InterviewPlan(Base):
    __tablename__ = "interview_plans"

    id: Mapped[uuid.UUID] = _pk()
    round_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interview_rounds.id", ondelete="CASCADE"), index=True
    )
    request_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    source_key: Mapped[str | None] = mapped_column(String(64))
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB)
    progress: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(30))
    queue_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("queue_tasks.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_interview_plans_source_key", "source_key"),)


class InterviewJudgment(Base):
    __tablename__ = "interview_judgments"

    id: Mapped[uuid.UUID] = _pk()
    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interview_plans.id", ondelete="CASCADE"), index=True
    )
    requirement: Mapped[str] = mapped_column(String(64))
    evidence: Mapped[str] = mapped_column(String(64))
    grade: Mapped[int]
    __table_args__ = (
        UniqueConstraint("plan_id", "requirement", "evidence", name="uq_interview_judgment"),
    )


class InterviewVector(Base):
    """A disposable search vector with durable provenance in InterviewPlan."""

    __tablename__ = "interview_vectors"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interview_plans.id", ondelete="CASCADE"), index=True
    )
    source_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(64))
    embedding: Mapped[list[float]] = mapped_column(HALFVEC(LARGE_EMBEDDING_DIM))

    __table_args__ = (
        UniqueConstraint("plan_id", "source_id", "model", name="uq_interview_vector_source"),
    )
