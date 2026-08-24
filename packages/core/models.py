"""Authoritative SQLAlchemy models — CLAUDE.md §5.

Schema changes start here, then get an Alembic revision generated against them.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
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
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

#: Dimension of BAAI/bge-small-en-v1.5 embeddings.
EMBEDDING_DIM = 384


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _pk()
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    created_at: Mapped[datetime] = _created_at()

    candidates: Mapped[list[Candidate]] = relationship(back_populates="user")


class Candidate(Base):
    __tablename__ = "candidates"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    email_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="self")
    managed_alias: Mapped[str | None] = mapped_column(String(320))
    #: Vault handle, never the secret itself. See packages/core/vault.py.
    secrets_ref: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = _created_at()

    user: Mapped[User] = relationship(back_populates="candidates")

    __table_args__ = (
        CheckConstraint("email_mode IN ('managed', 'self')", name="ck_candidates_email_mode"),
    )


class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[uuid.UUID] = _pk()
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    base_resume_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("resumes.id", ondelete="SET NULL")
    )
    phone: Mapped[str | None] = mapped_column(String(50))
    location: Mapped[str | None] = mapped_column(String(200))
    #: Copied verbatim onto applications — never LLM-generated. CLAUDE.md §2.2.
    work_auth: Mapped[str | None] = mapped_column(String(200))
    needs_sponsorship: Mapped[bool | None] = mapped_column(Boolean)
    links_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    salary_expectation: Mapped[str | None] = mapped_column(String(100))
    answers_kv_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    min_match_score: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0.75")
    )
    #: Opt-in per profile, and only above min_match_score. CLAUDE.md §2.3.
    auto_submit: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = _created_at()


class Resume(Base):
    __tablename__ = "resumes"

    id: Mapped[uuid.UUID] = _pk()
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    #: Path within the storage interface. The file itself never enters the DB.
    storage_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    parsed_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    #: Set only on tailored résumés — packages/tailor/cache.py. NULL on an
    #: uploaded base résumé and on anything tailored for a posting that had no
    #: content hash to key on, so a NULL here means "do not reuse this", never
    #: "reusable for anything".
    tailored_key: Mapped[str | None] = mapped_column(String(64), index=True)
    #: The posting this was written for. `tailored_key` already covers the same
    #: posting, but it is a sha256 over five inputs and cannot be read back —
    #: so given a PDF, the only way to name the job was to reverse-join through
    #: `applications` or `matches`, and a résumé that was rendered but never
    #: attached to either had no answer at all. A document that cannot say what
    #: it was written for is hard to audit and easy to send to the wrong place.
    #: SET NULL rather than CASCADE: losing the posting must not delete the
    #: résumé that was actually sent to an employer.
    tailored_for_posting_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("postings.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = _created_at()


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    domain: Mapped[str | None] = mapped_column(String(300))
    careers_url: Mapped[str | None] = mapped_column(Text)
    ats_type: Mapped[str | None] = mapped_column(String(50))
    #: Floor is enforced in the crawler too; never configure below 60s.
    poll_interval_s: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("3600")
    )
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: sha256 of the last board response. When it is unchanged, the crawler
    #: skips parsing entirely instead of re-hashing every posting.
    board_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created_at()


class Posting(Base):
    __tablename__ = "postings"

    id: Mapped[uuid.UUID] = _pk()
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE")
    )
    ats_type: Mapped[str | None] = mapped_column(String(50))
    external_id: Mapped[str | None] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(Text, nullable=False)
    #: Text, not String(n). Both are typed by an employer into a form with no
    #: length limit, and the first real crawl proved the guess wrong: a
    #: Greenhouse posting at SumUp lists every US state it hires in, 561
    #: characters of location against a 300-character column. That did not
    #: truncate — asyncpg raised StringDataRightTruncationError and the whole
    #: cycle aborted, losing 108 other companies' postings to one row.
    #: There is no correct maximum here, so there is no maximum.
    title: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    description_raw: Mapped[str | None] = mapped_column(Text)
    description_embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    #: Change detection — an unchanged hash means the crawler emits nothing.
    content_hash: Mapped[str | None] = mapped_column(String(64))
    #: When the *source* says the posting went up. Distinct from first_seen_at,
    #: which is when the crawler noticed it. The gap between them is our lag,
    #: and it is the only evidence that poll_interval_s is set sensibly.
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = _created_at()
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # No vector index. At ~50 companies the corpus is 500–5k postings, where an
    # exact scan is both faster and more accurate than approximate search, and
    # an ivfflat index built on an empty table cannot cluster at all. Add one
    # (HNSW, not ivfflat) once the row count actually justifies it.
    __table_args__ = (
        Index("ix_postings_first_seen_at", text("first_seen_at DESC")),
        Index("ix_postings_published_at", text("published_at DESC")),
        Index("ix_postings_content_hash", "content_hash"),
    )


class Match(Base):
    __tablename__ = "matches"

    id: Mapped[uuid.UUID] = _pk()
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    posting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("postings.id", ondelete="CASCADE"), nullable=False
    )
    score: Mapped[float] = mapped_column(Float, nullable=False)
    reasons_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: What the owner did with this posting: `interested`, `skipped`, or NULL
    #: for not yet seen. Kept beside the score rather than in its own table
    #: because it is the same fact from the other side — the score is what the
    #: machine thinks of this posting for this profile, and this is what the
    #: owner thinks. Storing them together is what lets one be checked against
    #: the other.
    decision: Mapped[str | None] = mapped_column(String(20))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: A résumé tailored for this posting ahead of time, so the apply pipeline
    #: does not wait on a model. Keyed here rather than on Application because
    #: tailoring depends on the job description, and a Match is exactly one
    #: (profile, posting) pair — the same key the work is done against.
    tailored_resume_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("resumes.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        Index("ix_matches_profile_score", "profile_id", text("score DESC")),
        # The swipe feed's only query: undecided matches for one profile. A
        # partial index because decided rows are the ones that accumulate, and
        # the feed never looks at them.
        Index(
            "ix_matches_profile_undecided",
            "profile_id",
            postgresql_where=text("decision IS NULL"),
        ),
        # Re-scoring looks up existing rows in memory and updates them; without
        # this a concurrent run could insert a second row for the same pair and
        # the owner's decision would silently attach to whichever copy the
        # query happened to return.
        UniqueConstraint("profile_id", "posting_id", name="uq_matches_profile_posting"),
    )


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[uuid.UUID] = _pk()
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    posting_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("postings.id", ondelete="SET NULL")
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    ats: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default=text("'queued'"))
    failure_reason: Mapped[str | None] = mapped_column(String(50))
    #: Carries the exact unanswerable question text when parked. CLAUDE.md §2.4.
    review_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    tailored_resume_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("resumes.id", ondelete="SET NULL")
    )
    cover_letter_ref: Mapped[str | None] = mapped_column(String(500))
    receipt_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: What the employer did after submission. Separate from `status`, which
    #: tracks our automation and ends at `submitted`. See enums.Outcome.
    outcome: Mapped[str | None] = mapped_column(String(30))
    outcome_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    events: Mapped[list[ApplicationEvent]] = relationship(
        back_populates="application", order_by="ApplicationEvent.at"
    )

    __table_args__ = (
        UniqueConstraint("candidate_id", "url", name="uq_applications_candidate_url"),
        Index("ix_applications_status", "status"),
    )


class ApplicationEvent(Base):
    """Append-only audit log. Rows are never updated or deleted."""

    __tablename__ = "application_events"

    id: Mapped[uuid.UUID] = _pk()
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    at: Mapped[datetime] = _created_at()

    application: Mapped[Application] = relationship(back_populates="events")

    __table_args__ = (Index("ix_application_events_application_id", "application_id"),)


class InboundMessage(Base):
    __tablename__ = "inbound_messages"

    id: Mapped[uuid.UUID] = _pk()
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL")
    )
    from_addr: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    classification: Mapped[str | None] = mapped_column(String(50))
    #: How this message was tied to its application: "alias" (exact, from the
    #: +app tag we issued), "inferred" (matched on sender and content), or
    #: "unlinked". The record says how it knows, because an inferred link is
    #: a guess and must never be read as an exact one.
    link_method: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'unlinked'")
    )
    #: 0..1 for an inferred link; NULL when the link was exact or absent.
    link_confidence: Mapped[float | None] = mapped_column(Float)
    at: Mapped[datetime] = _created_at()


class Project(Base):
    """A project imported from an external source, for résumé inclusion.

    Not in CLAUDE.md §5 — added for GitHub project ingestion. These rows are
    *source facts*: they come from the owner's own account, not from a model,
    which is what makes putting them on a résumé compatible with §2.1. Every
    text field here is stored exactly as the source reported it.
    """

    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = _pk()
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    #: Where it came from. Only "github" today.
    source: Mapped[str] = mapped_column(String(30), nullable=False, server_default=text("'github'"))
    #: The source's own id, so re-syncing updates rather than duplicates.
    external_id: Mapped[str] = mapped_column(String(100), nullable=False)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(Text, nullable=False)
    homepage: Mapped[str | None] = mapped_column(Text)
    #: Verbatim from the source. Never generated — an empty description stays
    #: empty rather than being invented.
    description: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(50))
    topics_json: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    stars: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    forks: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    is_fork: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    is_private: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Owner's explicit choice. None means "decide by the ranking rules".
    include: Mapped[bool | None] = mapped_column(Boolean)
    #: Always put this one on the résumé, ahead of ranked picks.
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    synced_at: Mapped[datetime] = _created_at()
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("candidate_id", "source", "external_id", name="uq_projects_source_id"),
        Index("ix_projects_candidate", "candidate_id"),
    )


class QueueTask(Base):
    """Postgres-backed queue. Consumed with FOR UPDATE SKIP LOCKED.

    A claim takes a *lease* rather than a permanent lock: `locked_by` records
    which worker holds it and `lease_expires_at` when that claim goes stale. A
    worker that dies mid-task leaves an expired lease, which the next claim
    reclaims. Holding an unexpired lease is what makes a handler the exclusive
    owner of the task — see packages/core/queue.py.
    """

    __tablename__ = "queue_tasks"

    id: Mapped[uuid.UUID] = _pk()
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'pending'")
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Stable worker identity (WORKER_ID / hostname), so a restarted worker can
    #: recognize its own abandoned lease.
    locked_by: Mapped[str | None] = mapped_column(String(200))
    #: When the current claim goes stale and becomes reclaimable.
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        Index("ix_queue_tasks_status_run_after", "status", "run_after"),
        # The reclaim half of a claim scans for expired leases; without
        # this it is a sequential scan of every running task.
        Index("ix_queue_tasks_status_lease", "status", "lease_expires_at"),
    )
