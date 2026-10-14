"""One durable submit attempt per candidate/job, across retries and profiles.

The caller takes lock_candidate, checks the ledger, saves its fence and
COMMITS before the click. No DB transaction can atomically commit an ATS
click: unknown outcomes must stop for a person, never automatically retry.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Application, ApplicationEvent, AppliedPosting, Posting
from packages.core.submission_identity import submission_key_v1


async def lock_candidate(session: AsyncSession, candidate_id: uuid.UUID) -> None:
    """Serialize job identity decisions, including known cross-source copies."""
    digest = hashlib.sha256(f"submit:{candidate_id}".encode()).digest()[:8]
    key = int.from_bytes(digest, signed=True)
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


async def find_existing(
    session: AsyncSession,
    candidate_id: uuid.UUID,
    url: str,
    *,
    posting_id: uuid.UUID | None = None,
    application_id: uuid.UUID | None = None,
) -> AppliedPosting | None:
    """The ledger row for this job, preferring another application's.

    Another application's row outranks this one's own, whatever their ages:
    it is the job sent, or being sent, some other way. Read earliest-first, an
    older `released` row of this application's own won, and it clicked for a
    job another listing of it had already sent.
    """
    posting = (
        await session.get(Posting, posting_id)
        if posting_id
        else await session.scalar(select(Posting).where(Posting.url == url).limit(1))
    )
    predicates = [AppliedPosting.job_key == submission_key_v1(url)]
    if application_id is not None:
        predicates.append(AppliedPosting.application_id == application_id)
    if posting is not None:
        predicates.append(AppliedPosting.posting_id == posting.id)
        if posting.canonical_job_id and not posting.canonical_locked:
            members = select(Posting.id).where(
                Posting.canonical_job_id == posting.canonical_job_id,
                Posting.canonical_locked.is_(False),
            )
            predicates.append(AppliedPosting.posting_id.in_(members))
    query = select(AppliedPosting).where(
        AppliedPosting.candidate_id == candidate_id, or_(*predicates)
    )
    if application_id is not None:
        # False sorts first: rows of other applications before this one's own.
        query = query.order_by((AppliedPosting.application_id == application_id).asc())
    query = query.order_by(AppliedPosting.attempted_at).limit(1)
    return cast(AppliedPosting | None, await session.scalar(query))


async def blocking(session: AsyncSession, application: Application) -> AppliedPosting | None:
    """The row that stops this application being sent, read now, or None.

    Another application's row in any state, or this one's own once it is
    `submitted`. Its own `attempting`, `uncertain` or `released` row does not
    count here: approving is how an uncertain one is released. Approval asks
    this rather than trusting what a park saved, which only a new run clears.
    The caller holds `lock_candidate`.
    """
    row = await find_existing(
        session,
        application.candidate_id,
        application.url,
        posting_id=application.posting_id,
        application_id=application.id,
    )
    if row is None:
        return None
    if row.application_id != application.id or row.state == "submitted":
        return row
    return None


def through_group(row: AppliedPosting, url: str) -> bool:
    """Whether `row` matched another listing of a grouped job, not this URL.

    Then splitting the listing out of its group is a way out.
    """
    return row.job_key != submission_key_v1(url)


async def sent_through(session: AsyncSession, row: AppliedPosting) -> uuid.UUID:
    """The application a `submitted` row was sent through.

    Usually the row's own. When the owner marks one application sent and the
    job's row belongs to another, `record_manual` confirms that row, and
    `mark_submitted` names it on the event it writes for the one marked.
    """
    if row.source == "owner":
        marked = await session.scalar(
            select(ApplicationEvent.application_id)
            .where(
                ApplicationEvent.payload_json["confirmed_fence_application_id"].astext
                == str(row.application_id),
                ApplicationEvent.application_id != row.application_id,
            )
            .order_by(ApplicationEvent.at.desc())
            .limit(1)
        )
        if marked is not None:
            return marked
    return row.application_id


async def record(
    session: AsyncSession,
    application: Application,
    *,
    state: str,
    source: str,
) -> AppliedPosting:
    posting_id = application.posting_id
    if posting_id is None:
        posting_id = await session.scalar(
            select(Posting.id).where(Posting.url == application.url).limit(1)
        )
    row = AppliedPosting(
        application_id=application.id,
        candidate_id=application.candidate_id,
        posting_id=posting_id,
        job_key=submission_key_v1(application.url),
        url=application.url,
        state=state,
        source=source,
        confirmed_at=datetime.now(UTC) if state == "submitted" else None,
    )
    session.add(row)
    await session.flush()
    return row


async def record_manual(session: AsyncSession, application: Application) -> AppliedPosting:
    """The owner reports a fact; this does not click or authorize another click."""
    await lock_candidate(session, application.candidate_id)
    # Its own row first: `find_existing` prefers another application's, and
    # confirming that one would leave this one's own row unconfirmed.
    row = await session.get(AppliedPosting, application.id) or await find_existing(
        session,
        application.candidate_id,
        application.url,
        posting_id=application.posting_id,
        application_id=application.id,
    )
    if row is None:
        return await record(session, application, state="submitted", source="owner")
    row.state = "submitted"
    row.source = "owner"
    row.confirmed_at = datetime.now(UTC)
    return row
