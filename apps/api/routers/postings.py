"""Posting routes — read-only search over indexed postings.

Nothing writes to this table until the Phase 5 crawler lands, so on a fresh
install these endpoints correctly return nothing. That is worth saying out
loud rather than letting an empty list read as "no matches found".
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import SessionDep
from apps.api.errors import ApiError
from packages.core.enums import ErrorCode
from packages.core.models import Posting, Profile, Resume
from packages.core.schemas import (
    AtsFindingOut,
    PostingAtsOut,
    PostingOut,
    PostingSearchOut,
)
from packages.core.schemas_jobs import (
    PostingHistoryOut,
    PostingSourceOut,
    PostingVersionOut,
    VersionChangeOut,
)
from packages.matching.pick_resume import choose_base_resume
from packages.tailor import ats as ats_scorer
from packages.tailor.parse import ParsedResume

router = APIRouter(prefix="/postings", tags=["postings"])


@router.get("", response_model=PostingSearchOut)
async def search_postings(
    session: SessionDep,
    q: str = "",
    location: str | None = None,
    ats: str | None = None,
    limit: int = 20,
    include_closed: bool = False,
) -> PostingSearchOut:
    """Search indexed postings by title, location, or description.

    Plain substring matching. Semantic search over `description_embedding`
    arrives with Phase 5, alongside the crawler that populates this table.
    """
    limit = max(1, min(limit, 100))

    total = await session.scalar(select(func.count()).select_from(Posting)) or 0

    stmt = select(Posting).order_by(Posting.first_seen_at.desc()).limit(limit)
    if not include_closed:
        stmt = stmt.where(Posting.closed_at.is_(None))
    if ats:
        stmt = stmt.where(Posting.ats_type == ats)
    if location:
        loc_pattern = f"%{location.strip()}%"
        stmt = stmt.where(Posting.location.ilike(loc_pattern))
    if q.strip():
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Posting.title.ilike(pattern),
                Posting.location.ilike(pattern),
                Posting.description_raw.ilike(pattern),
            )
        )

    rows = list((await session.scalars(stmt)).all())

    note = None
    if total == 0:
        note = (
            "No postings are indexed yet. The crawler that populates this table "
            "arrives in Phase 5; until then, apply to a URL directly."
        )

    return PostingSearchOut(
        results=[PostingOut.model_validate(row) for row in rows],
        total_indexed=total,
        note=note,
    )


@router.get("/{posting_id}", response_model=PostingOut)
async def get_posting(posting_id: uuid.UUID, session: SessionDep) -> Posting:
    posting = await session.get(Posting, posting_id)
    if posting is None:
        raise ApiError(ErrorCode.NOT_FOUND, "posting not found")
    return posting


@router.get("/{posting_id}/history", response_model=PostingHistoryOut)
async def posting_history(posting_id: uuid.UUID, session: SessionDep) -> PostingHistoryOut:
    """Every source listing this requisition, and what this listing said over time.

    Sources carry their own decisions and applications, so grouping never hides
    that one copy was already applied to or skipped.
    """
    posting = await session.get(Posting, posting_id)
    if posting is None:
        raise ApiError(ErrorCode.NOT_FOUND, "posting not found")
    return await _history(session, posting)


@router.post("/{posting_id}/split", response_model=PostingHistoryOut)
async def split_posting(posting_id: uuid.UUID, session: SessionDep) -> PostingHistoryOut:
    """Take this listing out of its group, for good — the owner's correction of a merge."""
    from packages.matching.canonical import split

    posting = await session.get(Posting, posting_id)
    if posting is None:
        raise ApiError(ErrorCode.NOT_FOUND, "posting not found")
    await split(session, posting)
    await session.commit()
    return await _history(session, posting)


async def _history(session: AsyncSession, posting: Posting) -> PostingHistoryOut:
    from packages.core.models import Application, Match
    from packages.core.models_jobs import CanonicalJob
    from packages.matching.canonical import source_key
    from packages.matching.versions import describe_pay, diff, history, snapshot

    members = [posting]
    evidence: dict[str, str] = {}
    if posting.canonical_job_id is not None:
        members = list(
            (
                await session.scalars(
                    select(Posting)
                    .where(Posting.canonical_job_id == posting.canonical_job_id)
                    .order_by(Posting.first_seen_at)
                )
            ).all()
        )
        group = await session.get(CanonicalJob, posting.canonical_job_id)
        evidence = {e["posting_id"]: e["reason"] for e in (group.evidence_json if group else [])}

    ids = [member.id for member in members]
    decisions: dict[uuid.UUID, list[str]] = {}
    for match_posting, decision in (
        await session.execute(
            select(Match.posting_id, Match.decision).where(
                Match.posting_id.in_(ids), Match.decision.is_not(None)
            )
        )
    ).all():
        decisions.setdefault(match_posting, []).append(decision)
    statuses: dict[uuid.UUID, list[str]] = {}
    for member in members:
        rows = await session.scalars(
            select(Application.status).where(
                (Application.posting_id == member.id) | (Application.url == member.url)
            )
        )
        statuses[member.id] = list(rows.all())

    versions = await history(session, posting.id)
    rendered = [
        PostingVersionOut(
            version=version.version,
            captured_at=version.captured_at,
            pay=describe_pay(snapshot(version)),
            changes=[
                VersionChangeOut(**change.as_dict())
                for change in (
                    diff(snapshot(versions[index - 1]), snapshot(version)) if index else []
                )
            ],
        )
        for index, version in enumerate(versions)
    ]
    return PostingHistoryOut(
        posting_id=posting.id,
        title=posting.title,
        canonical_job_id=posting.canonical_job_id,
        locked=posting.canonical_locked,
        sources=[
            PostingSourceOut(
                posting_id=member.id,
                url=member.url,
                ats_type=member.ats_type,
                source=source_key(member),
                title=member.title,
                location=member.location,
                closed=member.closed_at is not None,
                decisions=decisions.get(member.id, []),
                application_statuses=statuses.get(member.id, []),
                evidence=evidence.get(str(member.id)),
            )
            for member in members
        ],
        versions=rendered,
    )


@router.get("/{posting_id}/ats", response_model=PostingAtsOut)
async def posting_ats(
    posting_id: uuid.UUID,
    profile_id: uuid.UUID,
    session: SessionDep,
) -> PostingAtsOut:
    """How an ATS would read the owner's résumé against this posting.

    §23's "before" half, available while the decision to apply is still open.
    `/review` has carried this since tailoring landed, but only *after* an
    application exists — by which point the owner has already committed the
    slot. On `/matches` there was a rubric saying how well the job fits them
    and nothing saying how well their résumé reads to the machine that
    screens it.

    Computed on request rather than stored on the `Match` row. It is a
    property of the résumé and the posting, both of which change under it —
    an edit on `/review` or a re-crawl would leave a stored score describing
    documents that no longer exist, which is the failure mode this file's own
    `AtsChange` docstring avoids by storing a score that *is* pinned to a run.
    Here nothing is pinned, so nothing should be cached.

    Scores the résumé `choose_base_resume` would actually start from, not the
    profile's default. Those differ as soon as the owner has more than one
    base résumé, and putting a number on screen for a file the employer would
    never receive is the defect CLAUDE.md §15 records twice.
    """
    posting = await session.get(Posting, posting_id)
    if posting is None:
        raise ApiError(ErrorCode.NOT_FOUND, "posting not found")

    profile = await session.get(Profile, profile_id)
    if profile is None:
        raise ApiError(ErrorCode.NOT_FOUND, "profile not found")

    description = posting.description_raw or ""
    # The same haystack the apply pipeline builds, so the pick matches.
    haystack = "\n".join(part for part in (posting.title, posting.location, description) if part)

    choice = await choose_base_resume(session, profile, haystack)
    if choice is None:
        raise ApiError(
            ErrorCode.INVALID_REQUEST,
            "this profile has no base résumé to score — upload one on the résumés page",
        )

    resume = await session.get(Resume, choice.resume_id)
    if resume is None or not resume.parsed_json:
        raise ApiError(
            ErrorCode.INVALID_REQUEST,
            "the selected résumé has no parsed content to score",
        )

    parsed = ParsedResume.model_validate(resume.parsed_json)
    report = ats_scorer.score(parsed, description)

    return PostingAtsOut(
        posting_id=posting.id,
        resume_id=choice.resume_id,
        resume_version=choice.version,
        resume_reason=choice.reason,
        parse=report.parse,
        keywords=report.keywords,
        scored_against_posting=report.scored_against_posting,
        supported=report.supported,
        missing=report.missing,
        findings=[
            AtsFindingOut(code=f.code, detail=f.detail, cost=f.cost, line=f.line)
            for f in report.findings
        ],
    )
