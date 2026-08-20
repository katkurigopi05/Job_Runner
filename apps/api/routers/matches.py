"""Match routes — the read side of scoring.

`packages/matching/` scores postings and writes Match rows during a crawl, and
nothing could read them back, so §9 Phase 5's "match feed in the dashboard" had
a working scorer and no feed.

Every match carries the breakdown that produced its score. A feed that shows a
number and not the reasoning is a ranking the owner has to take on trust, and
the score decides what gets applied to.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query
from sqlalchemy import select

from apps.api.deps import SessionDep
from apps.api.errors import ApiError
from packages.core.enums import ErrorCode
from packages.core.models import Application, Match, Posting, Profile
from packages.core.schemas import MatchOut

router = APIRouter(prefix="/matches", tags=["matches"])

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


@router.get("", response_model=list[MatchOut])
async def list_matches(
    session: SessionDep,
    profile_id: uuid.UUID | None = None,
    min_score: float = Query(default=0.0, ge=0.0, le=1.0),
    include_applied: bool = True,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> list[MatchOut]:
    """Scored postings, best first.

    `include_applied=false` hides postings already applied to, which is what
    the feed wants by default once a search is under way — the interesting
    question is what is left, not what is done.
    """
    if profile_id is not None and await session.get(Profile, profile_id) is None:
        raise ApiError(ErrorCode.NOT_FOUND, "profile not found")

    stmt = (
        select(Match, Posting)
        .join(Posting, Posting.id == Match.posting_id)
        .where(Match.score >= min_score)
        .order_by(Match.score.desc())
        .limit(limit)
    )
    if profile_id is not None:
        stmt = stmt.where(Match.profile_id == profile_id)

    rows = (await session.execute(stmt)).all()

    applied_urls = set(
        (await session.scalars(select(Application.url))).all() if not include_applied else []
    )

    feed: list[MatchOut] = []
    for match, posting in rows:
        if not include_applied and posting.url in applied_urls:
            continue
        reasons = match.reasons_json or {}
        feed.append(
            MatchOut(
                id=match.id,
                profile_id=match.profile_id,
                posting_id=posting.id,
                score=match.score,
                title=posting.title,
                location=posting.location,
                url=posting.url,
                ats_type=posting.ats_type,
                first_seen_at=posting.first_seen_at,
                closed=posting.closed_at is not None,
                title_similarity=float(reasons.get("title_similarity") or 0.0),
                body_similarity=float(reasons.get("body_similarity") or 0.0),
                matched_terms=list(reasons.get("matched_terms") or []),
                missing_terms=list(reasons.get("missing_terms") or []),
                legitimacy=dict(reasons.get("legitimacy") or {}),
                rubric=dict(reasons.get("rubric") or {}),
                excluded_by=list(reasons.get("excluded_by") or []),
            )
        )
    return feed
