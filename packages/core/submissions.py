"""One durable submit attempt per candidate/job, across retries and profiles.

The caller takes lock_candidate, checks the ledger, saves its fence and
COMMMITS before the click. No DB transaction can atomically commit an ATS
click: unknown outcomes must stop for a person, never automatically retry.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Application, AppliedPosting, Posting
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
    return cast(
        AppliedPosting | None,
        await session.scalar(
            select(AppliedPosting)
            .where(AppliedPosting.candidate_id == candidate_id, or_(*predicates))
            .order_by(AppliedPosting.attempted_at)
            .limit(1)
        ),
    )


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


async def record_manual(session: AsyncSession, application: Application) -> None:
    """The owner reports a fact; this does not click or authorize another click."""
    await lock_candidate(session, application.candidate_id)
    row = await find_existing(
        session,
        application.candidate_id,
        application.url,
        posting_id=application.posting_id,
        application_id=application.id,
    )
    if row is None:
        await record(session, application, state="submitted", source="owner")
    else:
        row.state = "submitted"
        row.confirmed_at = datetime.now(UTC)
