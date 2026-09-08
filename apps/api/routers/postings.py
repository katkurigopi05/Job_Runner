"""Posting routes — read-only search over indexed postings.

Nothing writes to this table until the Phase 5 crawler lands, so on a fresh
install these endpoints correctly return nothing. That is worth saying out
loud rather than letting an empty list read as "no matches found".
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from sqlalchemy import func, or_, select

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
