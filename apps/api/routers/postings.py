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
from packages.core.models import Posting
from packages.core.schemas import PostingOut, PostingSearchOut

router = APIRouter(prefix="/postings", tags=["postings"])


@router.get("", response_model=PostingSearchOut)
async def search_postings(
    session: SessionDep,
    q: str = "",
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
