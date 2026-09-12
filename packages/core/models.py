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
    """Base class for all SQLAlchemy models."""

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
    """A user account owning one or more candidates."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _pk()
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    created_at: Mapped[datetime] = _created_at()

    candidates: Mapped[list[Candidate]] = relationship(back_populates="user")


class Candidate(Base):
    """A person applying to jobs, owned by a user."""

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
    """A candidate's profile containing resume and application preferences."""

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
    #: The rung the owner is applying at — one of `filters.SENIORITY_LEVELS`.
    #:
    #: NULL means "do not filter on level", which is the shipped behaviour and
    #: what every existing row gets. `filters.seniority_ok` returns True for a
    #: None target, so the hard filter passes everything; until this column
    #: existed no production caller set a target at all, and `target_seniority`
    #: was reachable only from the benchmark.
    #:
    #: That gap has a number on it. On the Gate 5 labeled set, arming the
    #: target takes P@10 from 0.900 to 1.000 — the posting it removes is a
    #: Junior Backend Engineer with an otherwise excellent technology match,
    #: which is exactly the kind of role a cosine score cannot refuse on its
    #: own. It stays opt-in because the right rung is the owner's to state,
    #: not something to infer from a résumé (CLAUDE.md §1).
    target_seniority: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = _created_at()


class Resume(Base):
    """A candidate's resume, either base or tailored for a posting."""

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
    #: Which model wrote this document — "gemini", or "ollama:llama3.1" when the
    #: allowance ran out mid-run and §7's fallback answered. Stored on the
    #: résumé rather than derived at approval time because the run that tailored
    #: it is gone by the time a cache hit or an overnight batch serves this row
    #: to a different application, and those are the paths where the review
    #: screen would otherwise show nothing at all.
    #: NULL means unrecorded — a base résumé, or one tailored before this
    #: column existed. Never a guess: a wrong model name is worse than a blank,
    #: because the owner would have no reason to doubt it.
    tailored_by: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = _created_at()


class Company(Base):
    """A company with a careers page to poll."""

    __tablename__ = "companies"

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    domain: Mapped[str | None] = mapped_column(String(300))
    careers_url: Mapped[str | None] = mapped_column(Text)
    ats_type: Mapped[str | None] = mapped_column(String(50))
    #: The company's identifier *on its ATS* — the `acme` in
    #: `boards-api.greenhouse.io/v1/boards/acme/jobs`. It lived only in
    #: `seeds/companies.yaml` until now, which was fine while a cycle was one
    #: pass over the YAML: `crawl_company` was handed a `CompanySeed` and read
    #: the slug off it.
    #:
    #: A per-company queue task cannot do that. It carries a company id, and
    #: the handler that picks it up has to rebuild the board URL from the row
    #: alone — the YAML may have been edited, reordered, or not be the thing
    #: that enqueued the task at all. Without this column the row cannot say
    #: which board it stands for.
    #:
    #: Nullable because rows exist that were never seeded: companies created
    #: by discovery promotion before they resolve to a board, and the fixtures
    #: in the test suite.
    slug: Mapped[str | None] = mapped_column(String(200))
    #: Floor is enforced in the crawler too; never configure below 60s.
    poll_interval_s: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("3600")
    )
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: sha256 of the last board response. When it is unchanged, the crawler
    #: skips parsing entirely instead of re-hashing every posting.
    board_hash: Mapped[str | None] = mapped_column(String(64))

    # --- What we know about the source, and how we came to know it ---------
    #
    # `careers_url` alone could not answer "is this a career page or a guess".
    # A Google search link imported from a spreadsheet sat in the same column
    # as a Greenhouse board polled successfully for a month, so any screen
    # reading it presented the first as the second.
    #: A `SourceStatus`. Nothing promotes a row except evidence.
    source_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'unverified'")
    )
    #: What justified `source_status` — the method, what answered, when. Kept
    #: as a record rather than a flag because "verified" with no account of
    #: how is the claim this column exists to stop being taken on trust.
    source_evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    source_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Why the last discovery attempt produced nothing. Distinct from
    #: `CompanyCrawlState.last_error`, which is about polling a board we have;
    #: this is about failing to find one at all.
    discovery_failure: Mapped[str | None] = mapped_column(Text)

    # --- Provenance, for a row that came from a spreadsheet ----------------
    #
    # The supplied values are never overwritten by discovery. A careers URL we
    # resolved to a board is an improvement on what the sheet said, not a
    # correction of it — and when a resolution turns out to be wrong, the only
    # way back is the original text.
    supplied_website: Mapped[str | None] = mapped_column(Text)
    supplied_career_url: Mapped[str | None] = mapped_column(Text)
    #: File and 1-based row the company was imported from. A duplicate name
    #: three thousand rows in is otherwise unfindable.
    source_file: Mapped[str | None] = mapped_column(Text)
    source_row: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint(
            "source_status IN ('no_website', 'hint', 'unverified', 'verified', 'failed')",
            name="ck_companies_source_status",
        ),
        # The import and the dashboard both group by this, over the whole
        # registry, every time they run.
        Index("ix_companies_source_status", "source_status"),
        # The scheduler asks "which companies are due?" every tick. At 29 rows
        # that is a sequential scan nobody notices; at 3,500 it is the query
        # that runs most often in the whole system.
        Index("ix_companies_last_polled_at", "last_polled_at"),
        # Resolution and promotion both ask "do we already have this board?"
        # before writing. Not unique: two rows may legitimately share a slug
        # across different ATS vendors, and enforcing otherwise would make a
        # collision an import failure rather than a fact to look at.
        Index("ix_companies_ats_slug", "ats_type", "slug"),
    )


class Posting(Base):
    """A job posting from a company's careers page."""

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
    #: Which embedder produced the vector above, and which corpus statistics
    #: it was weighted by. Both are part of the vector's identity: a cosine
    #: between two vectors from different models, or from the same model under
    #: different IDF weights, is noise rather than a low score — and noise
    #: that reports itself as a number nobody questions.
    #:
    #: `embedding_revision` is NULL for an unweighted embedder, which has no
    #: corpus statistics to be stale against.
    embedding_model: Mapped[str | None] = mapped_column(String(64))
    embedding_revision: Mapped[int | None] = mapped_column(Integer)
    #: Change detection — an unchanged hash means the crawler emits nothing.
    content_hash: Mapped[str | None] = mapped_column(String(64))
    #: When the *source* says the posting went up. Distinct from first_seen_at,
    #: which is when the crawler noticed it. The gap between them is our lag,
    #: and it is the only evidence that poll_interval_s is set sensibly.
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = _created_at()
    #: The last cycle that found this posting still listed on its board.
    #:
    #: Distinct from `content_hash`, which answers "did it change", and from
    #: `first_seen_at`, which answers "when did we notice it". Neither answers
    #: "is it still there", and an unchanged posting used to update nothing at
    #: all — so a posting last confirmed an hour ago and one last confirmed in
    #: March were the same row.
    #:
    #: That gap is what `_close_missing` has to work around. It closes by set
    #: difference within a single cycle, which is only sound when one pass saw
    #: the whole board; under a dispatcher, several workers and a paginated
    #: board, "absent from this response" stops meaning "absent from the
    #: board". A timestamp is the durable form of the same question.
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # No vector index. At ~50 companies the corpus is 500–5k postings, where an
    # exact scan is both faster and more accurate than approximate search, and
    # an ivfflat index built on an empty table cannot cluster at all. Add one
    # (HNSW, not ivfflat) once the row count actually justifies it.
    __table_args__ = (
        Index("ix_postings_first_seen_at", text("first_seen_at DESC")),
        Index("ix_postings_published_at", text("published_at DESC")),
        Index("ix_postings_content_hash", "content_hash"),
        # Every crawl of every company runs two queries keyed on this column
        # — load the existing set, then load the open set — and neither had an
        # index. That is twice per cycle per company: 58 sequential scans of
        # the whole posting table at 29 companies, 7,000 at 3,500.
        Index("ix_postings_company_id", "company_id"),
        # What makes `ON CONFLICT` possible, and therefore what turns `_store`
        # from a read-then-branch loop into one statement.
        #
        # NULLs stay distinct under this, which is the behaviour we want
        # rather than a limitation to work around: a row with no external_id
        # has nothing to be deduplicated *by*, and the benchmark corpus and
        # the older fixtures hold such rows. Every row the crawler writes has
        # one — `ExtractedPosting.external_id` is a required `str`.
        UniqueConstraint("company_id", "external_id", name="uq_postings_company_external_id"),
    )


class CrawlerHostBudget(Base):
    """One row per host: when it may next be touched. CLAUDE.md §2.6.

    `HostRateLimiter` keeps the same facts in a dict, which is correct and
    sufficient for exactly as long as one process does all the crawling. The
    moment companies are dispatched to several workers, each worker has its
    own dict and its own idea of when a host was last hit — so N workers make
    the effective floor the floor divided by N, and the people who find out
    are at the far end of it.

    That is worst precisely where it matters most. A shared ATS API serves
    thousands of boards from one host at the amended 2s floor, so it is the
    host every worker is talking to at once.

    Keyed on the host string produced by `ratelimit.host_key`, not on a URL or
    an origin: §2.6 counts requests against a machine.

    `next_allowed_at` is a reservation rather than a record of the last
    request. A caller takes the next free slot and pushes the marker out by
    one delay, all inside a single statement, so two workers arriving together
    get two different slots instead of both reading the same "last request"
    and both concluding they may go.
    """

    __tablename__ = "crawler_host_budgets"

    host: Mapped[str] = mapped_column(String(255), primary_key=True)
    #: The next instant at which *some* caller may request this host. Written
    #: with `clock_timestamp()`, never `now()`: `now()` is the transaction's
    #: start time, identical for every statement inside it, which would hand
    #: every host in a cycle the same instant.
    next_allowed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: The delay currently applied to this host — the floor, or more if the
    #: site's own `Crawl-delay` asked for more. Shared so that a rule one
    #: worker read from robots.txt binds the others too.
    delay_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[datetime] = _created_at()


class CrawlRun(Base):
    """One crawl cycle, kept after the process that ran it has gone.

    `CrawlReport` already summarises a cycle, but it is an in-memory object
    owned by whichever worker ran the loop: it lives exactly as long as the
    process and is then reduced to a single log line. That is enough while a
    cycle *is* one process making one pass over the seed file.

    It stops being enough twice over at 3,500 companies. A cycle that takes
    hours will span worker restarts, and once companies are dispatched as
    individual tasks no single process sees the whole cycle at all — so there
    is no object anywhere that could hold the summary. The questions the owner
    actually asks ("did last night's run finish", "is the new-posting rate
    falling because hiring slowed or because an extractor broke") are about a
    cycle as a whole, over time, and they need a row.
    """

    __tablename__ = "crawl_runs"

    id: Mapped[uuid.UUID] = _pk()
    #: What started it: `scheduled`, `manual`, or `forced` (a cycle run with
    #: change detection bypassed).
    trigger: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'running'")
    )
    started_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: How many companies the run intended to poll. Recorded up front so a run
    #: that died halfway is distinguishable from one that had little to do —
    #: both otherwise show a small `companies_fetched` and no error.
    companies_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    companies_fetched: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    companies_skipped: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    companies_failed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    postings_new: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    postings_updated: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    postings_closed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    #: Companies whose board fetched cleanly and parsed to nothing while we
    #: still held open postings — `crawl._close_missing`'s refusal case. Kept
    #: by name because the useful form of this is "which ones", and because a
    #: count of 0 is the thing worth being able to prove afterwards.
    suspect_companies: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'completed', 'aborted')", name="ck_crawl_runs_status"
        ),
        CheckConstraint(
            "trigger IN ('scheduled', 'manual', 'forced')", name="ck_crawl_runs_trigger"
        ),
        Index("ix_crawl_runs_started_at", text("started_at DESC")),
    )


class CompanyCrawlState(Base):
    """Scheduling and health for one company's board, kept off the registry row.

    Two things live here that `Company` cannot answer at scale.

    **When is this company next due.** `is_due` computes
    `last_polled_at + poll_interval_s` per row, which no index can help with
    because the interval varies by row. A scheduler asking "what is due" every
    tick would scan the whole registry each time. `next_due_at` is that sum,
    materialized and indexed, so the question becomes a range scan.

    **How badly is it going.** CLAUDE.md §9 records that 21 of the original 50
    seeds had left Greenhouse and returned 404 from both the board API and the
    rendered page. At 29 companies a handful of dead boards is a line in a
    report; at 3,500 it is a standing tax on every cycle, spending the rate
    limiter's budget on hosts that have not answered in months.
    `consecutive_failures` is what lets a failing board back off without being
    dropped — a 404 today may be a board that moved, not a company that died,
    and deleting the row would lose the evidence CLAUDE.md §9 deliberately
    keeps.

    Separate from `Company` rather than more columns on it, because the two
    have different writers. The registry is written by imports and by the
    owner curating `seeds/companies.yaml`; this is written by the crawler on
    every attempt. Keeping them apart means re-importing the registry cannot
    reset crawl health, and a crawl cannot silently edit the registry.

    `Company.board_hash` deliberately stays where it is. It identifies the
    *content* last seen, not the schedule, and moving it would mean two places
    holding crawl state during the transition — which is the parallel
    architecture this work is supposed to avoid.
    """

    __tablename__ = "company_crawl_states"

    id: Mapped[uuid.UUID] = _pk()
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    #: When this company may next be polled. NULL means "as soon as possible",
    #: which is the correct reading for a company that has never been crawled.
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Distinct from `last_attempt_at`: the gap between them is exactly how
    #: long a board has been failing, which is the number worth alerting on.
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    #: The last `CompanyResult` outcome: `ok`, `unchanged`, `skipped`,
    #: `blocked`, `error`, or `suspect`.
    last_status: Mapped[str | None] = mapped_column(String(20))
    last_error: Mapped[str | None] = mapped_column(Text)

    #: Discovery's own schedule, kept apart from the crawl's.
    #:
    #: A company with no board yet cannot be crawled, so it would never come
    #: due under `next_due_at` and would never be retried. And the two cadences
    #: are genuinely different: a board is polled hourly, while a careers page
    #: that yielded nothing is worth another look in days, not minutes.
    #:
    #: Here rather than on `Company` for the reason the rest of this table is:
    #: it is written on every attempt, and re-importing the registry must not
    #: reset it.
    discovery_next_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discovery_last_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discovery_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    #: The run that last touched this company. SET NULL so pruning old runs is
    #: a decision about history, not something that can orphan a company.
    last_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("crawl_runs.id", ondelete="SET NULL")
    )

    __table_args__ = (
        UniqueConstraint("company_id", name="uq_company_crawl_states_company"),
        # The scheduler's only query: the due ones, soonest first.
        Index("ix_company_crawl_states_next_due_at", "next_due_at"),
        # Discovery asks the same question of its own column.
        Index("ix_company_crawl_states_discovery_next_at", "discovery_next_at"),
    )


class CorpusStats(Base):
    """Document frequencies over the postings, at a point in time.

    Persisted rather than recomputed per pass because the numbers are part of
    a stored vector's identity. A statistic recomputed on every crawl would
    shift continuously, and every posting would be permanently stale against
    it — so it is rebuilt on a growth policy and stamped with a revision that
    only moves when it is rebuilt.
    """

    __tablename__ = "corpus_stats"

    id: Mapped[uuid.UUID] = _pk()
    #: Monotonic. What `Posting.embedding_revision` points at.
    revision: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    total_documents: Mapped[int] = mapped_column(Integer, nullable=False)
    #: term -> document count. Capped, see packages/matching/idf.py.
    counts_json: Mapped[dict[str, int]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class Match(Base):
    """A scored posting paired with a profile."""

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


class PostingLabel(Base):
    """A relevance grade the owner sat down and gave a posting.

    Deliberately its own table rather than a column on `Match`, and the reason
    is the whole point of the labeling loop.

    `packages/matching/feedback.py` records two weaknesses in swipe-derived
    labels: a swipe is **binary**, so it cannot tell "would apply" from "would
    drop everything for"; and it is **taken in feed order**, so it is only ever
    recorded for postings the ranker already surfaced — the model ends up
    graded on its own shortlist. A 0–3 scale on a `Match` row would fix the
    first and leave the second exactly as it is, while the label now claimed
    `provenance: owner` — the grade a benchmark trusts most. That is worse than
    not fixing it, because the bias would stop being visible.

    A `Match` row exists only for a posting that cleared the hard filters and
    got scored. Keying here on (profile, posting) instead means a posting the
    ranker buried, filtered out, or scored near zero can still be graded, which
    is the only way a label set can measure what the ranker is missing.
    """

    __tablename__ = "posting_labels"

    id: Mapped[uuid.UUID] = _pk()
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    posting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("postings.id", ondelete="CASCADE"), nullable=False
    )
    #: 0–3, per `labels.RELEVANCE_SCALE`. Checked in the database as well as in
    #: the schema: a grade outside the scale silently breaks `2**rel` gain in
    #: NDCG rather than raising, so it must not be storable.
    relevance: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Why this grade. What lets a disagreement be settled later rather than
    #: re-litigated from scratch — `LabeledPosting.note` carries it onward.
    note: Mapped[str | None] = mapped_column(Text)
    #: The score the ranker gave when this was graded, or NULL when it had no
    #: opinion. Stored rather than looked up later because re-scoring moves it,
    #: and "what did the ranker think at the moment a human disagreed" is the
    #: measurement — recomputing it answers a different question.
    score_at_label: Mapped[float | None] = mapped_column(Float)
    #: Which `active.Stream` offered this posting. The audit trail for the
    #: sampling bias the loop exists to avoid: a corpus that is all `uncertain`
    #: was drawn from the ranker's own shortlist and carries exactly the
    #: weakness `provenance: owner` is supposed to have escaped. Without this
    #: stored per row, that is unknowable after the fact.
    stream: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        # Re-grading overwrites. One grade per pair, so a corrected label
        # replaces the old one instead of both being exported.
        UniqueConstraint("profile_id", "posting_id", name="uq_posting_labels_profile_posting"),
        CheckConstraint("relevance BETWEEN 0 AND 3", name="ck_posting_labels_relevance_scale"),
        Index("ix_posting_labels_profile", "profile_id"),
    )


class Application(Base):
    """A job application in the pipeline."""

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
    """A recruiter reply received via email."""

    __tablename__ = "inbound_messages"

    id: Mapped[uuid.UUID] = _pk()
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL")
    )
    #: The RFC 5322 Message-ID, which is what makes re-delivery detectable.
    #: `route_message` de-duplicates on it: IMAP re-delivers, and a rejection
    #: recorded twice must not look like two rejections. It used to say that
    #: and key on (from_addr, application_id, subject) instead — a heuristic
    #: that collides on exactly the mail which legitimately repeats, so a
    #: resent OTP was dropped and its application sat in `needs_otp` holding
    #: the expired code. NULL only on rows written before this column existed.
    message_id: Mapped[str | None] = mapped_column(String(998))
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

    __table_args__ = (
        #: Unique so a duplicate cannot be inserted at all, and partial so the
        #: NULLs on rows written before the column existed do not collide with
        #: each other. `route_message` conflicts against it rather than
        #: reading first — a SELECT then an INSERT is two chances for two
        #: concurrent `handle_inbox` tasks to both pass.
        Index(
            "uq_inbound_messages_candidate_message",
            "candidate_id",
            "message_id",
            unique=True,
            postgresql_where=text("message_id IS NOT NULL"),
        ),
    )


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
