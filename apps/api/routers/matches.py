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
from datetime import UTC

from fastapi import APIRouter, Query
from sqlalchemy import select

from apps.api.deps import SessionDep
from apps.api.errors import ApiError
from packages.core.enums import ErrorCode
from packages.core.models import Application, Match, Posting, Profile
from packages.core.schemas import MatchOut
from packages.matching.search import (
    SENIORITY_ORDER,
    SearchFilters,
)
from packages.matching.search import matches as filter_matches

router = APIRouter(prefix="/matches", tags=["matches"])


def _lag_hours(posting: Posting) -> float | None:
    """How long the crawler took to notice, in hours.

    None when the board reports no publication date. That is an unmeasurable
    lag, not a lag of zero, and reporting it as zero would flatter the number
    the measurement exists to question.
    """
    if posting.published_at is None:
        return None
    published = posting.published_at
    if published.tzinfo is None:
        published = published.replace(tzinfo=UTC)
    seen = posting.first_seen_at
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=UTC)
    return round(max((seen - published).total_seconds(), 0.0) / 3600, 2)


DEFAULT_LIMIT = 50
MAX_LIMIT = 200


@router.get("", response_model=list[MatchOut])
async def list_matches(
    session: SessionDep,
    profile_id: uuid.UUID | None = None,
    min_score: float = Query(default=0.0, ge=0.0, le=1.0),
    include_applied: bool = True,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    # Search filters — what the owner asked to see. Deliberately not read from
    # the profile: narrowing a search must not change what goes on a form.
    keywords: str = "",
    locations: str = "",
    remote: bool | None = None,
    min_seniority: str | None = None,
    max_seniority: str | None = None,
    posted_within_days: int | None = Query(default=None, ge=1, le=3650),
    include_closed: bool = False,
) -> list[MatchOut]:
    """Scored postings, best first.

    `include_applied=false` hides postings already applied to, which is what
    the feed wants by default once a search is under way — the interesting
    question is what is left, not what is done.
    """
    if profile_id is not None and await session.get(Profile, profile_id) is None:
        raise ApiError(ErrorCode.NOT_FOUND, "profile not found")

    filters = SearchFilters(
        keywords=tuple(k.strip() for k in keywords.split(",") if k.strip()),
        locations=tuple(loc.strip() for loc in locations.split(",") if loc.strip()),
        remote=remote,
        min_seniority=min_seniority,
        max_seniority=max_seniority,
        posted_within_days=posted_within_days,
        include_closed=include_closed,
    )

    for level in (min_seniority, max_seniority):
        if level is not None and level not in SENIORITY_ORDER:
            raise ApiError(
                ErrorCode.INVALID_REQUEST,
                f"unknown seniority {level!r}; expected one of {', '.join(SENIORITY_ORDER)}",
            )

    # Filters are applied in Python rather than SQL because seniority and
    # remoteness are read out of text, not stored as columns. The limit is
    # applied after filtering so a narrow search still returns a full page.
    stmt = (
        select(Match, Posting)
        .join(Posting, Posting.id == Match.posting_id)
        .where(Match.score >= min_score)
        .order_by(Match.score.desc())
    )
    if profile_id is not None:
        stmt = stmt.where(Match.profile_id == profile_id)

    rows = (await session.execute(stmt)).all()

    applied_urls = set(
        (await session.scalars(select(Application.url))).all() if not include_applied else []
    )

    feed: list[MatchOut] = []
    for match, posting in rows:
        if len(feed) >= limit:
            break
        if not include_applied and posting.url in applied_urls:
            continue
        if not filter_matches(posting, filters).kept:
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
                published_at=posting.published_at,
                lag_hours=_lag_hours(posting),
                closed=posting.closed_at is not None,
                title_similarity=float(reasons.get("title_similarity") or 0.0),
                body_similarity=float(reasons.get("body_similarity") or 0.0),
                excluded_by=list(reasons.get("excluded_by") or []),
            )
        )
    return feed
