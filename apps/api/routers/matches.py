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
from datetime import UTC, datetime

from fastapi import APIRouter, Query
from sqlalchemy import func, select
from sqlalchemy.orm import defer
from starlette.concurrency import run_in_threadpool

from apps.api.deps import SessionDep
from apps.api.errors import ApiError
from packages.core.config import get_settings
from packages.core.enums import ErrorCode
from packages.core.models import Application, Match, Posting, Profile
from packages.core.schemas import (
    CalibrationOut,
    MatchDecision,
    MatchDecisionOut,
    MatchOut,
    MatchSummaryOut,
)
from packages.matching.locality import locality_of
from packages.matching.locality import rank as locality_rank
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

#: Below this many kept postings, a suggested threshold would be noise
#: dressed as a measurement.
MIN_DECISIONS_TO_SUGGEST = 10


@router.get("", response_model=list[MatchOut])
async def list_matches(
    session: SessionDep,
    profile_id: uuid.UUID | None = None,
    min_score: float = Query(default=0.0, ge=0.0, le=1.0),
    include_applied: bool = True,
    #: The swipe feed's filter: only what the owner has not ruled on yet.
    undecided_only: bool = False,
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
    #: None means "use the standing preference" (Settings.search_us_only).
    #: Pass false to look outside the US for one call.
    us_only: bool | None = None,
    allow_unknown_location: bool = True,
    allow_unknown_seniority: bool = False,
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
        us_only=get_settings().search_us_only if us_only is None else us_only,
        allow_unknown_location=allow_unknown_location,
        allow_unknown_seniority=allow_unknown_seniority,
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
        # The embedding is 384 floats per posting and this handler never reads
        # one — `MatchOut` has no field for it and `filter_matches` works off
        # title, location and description text. Left loaded, every request
        # pulled the entire corpus's vectors out of Postgres to discard them,
        # because the rows below are fetched unbounded (see the comment on
        # `kept`) and so the cost scaled with the whole match table rather
        # than with the page.
        #
        # `description_raw` stays loaded on purpose: the seniority and
        # clearance filters read it.
        .options(defer(Posting.description_embedding))
        .where(Match.score >= min_score)
        .order_by(Match.score.desc())
    )
    if profile_id is not None:
        stmt = stmt.where(Match.profile_id == profile_id)
    if undecided_only:
        stmt = stmt.where(Match.decision.is_(None))

    rows = (await session.execute(stmt)).all()

    applied_urls = set(
        (await session.scalars(select(Application.url))).all() if not include_applied else []
    )

    def _filter() -> list[tuple[Match, Posting]]:
        # Ordering happens before the limit, not during it. The SQL is ordered
        # by score, so truncating inside the loop would take the top `limit` by
        # score and only then sort those — a Bay Area posting at rank 51 would
        # never be seen, which is the opposite of what "California first" is for.
        selected = [
            (match, posting)
            for match, posting in rows
            if (include_applied or posting.url not in applied_urls)
            and filter_matches(posting, filters).kept
        ]
        if filters.us_only:
            selected.sort(
                key=lambda row: (locality_rank(locality_of(row[1].location)), -row[0].score)
            )
        return selected

    # Off the event loop, because this is the one genuinely CPU-bound step in
    # the API. Seniority and remoteness are read out of posting *text*, so the
    # filter cannot run in SQL, and it therefore scans every candidate row:
    # measured at 2.5s over 4050 matches carrying 27MB of description between
    # them, against 0.4s for the query that fetched them.
    #
    # Left inline, those 2.5s block the whole single-process API — not just this
    # request. Every other page, and the dashboard's own health poll, queued
    # behind it, which is why concurrent loads of /matches took 15-19s while the
    # endpoint alone took 2.9s. Moving it to a worker thread does not make it
    # faster; it stops one slow endpoint from freezing the rest of the app.
    #
    # Safe in a thread only because every attribute it touches is already
    # loaded: title, location, description_raw and url all came back with the
    # query above. `description_embedding` is deferred and must stay untouched —
    # reading it here would emit a lazy load from a thread that has no session.
    kept = await run_in_threadpool(_filter)

    feed: list[MatchOut] = []
    for match, posting in kept[:limit]:
        reasons = match.reasons_json or {}
        feed.append(
            MatchOut(
                id=match.id,
                profile_id=match.profile_id,
                posting_id=posting.id,
                score=match.score,
                decision=match.decision,
                decided_at=match.decided_at,
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
                matched_terms=list(reasons.get("matched_terms") or []),
                missing_terms=list(reasons.get("missing_terms") or []),
                legitimacy=dict(reasons.get("legitimacy") or {}),
                rubric=dict(reasons.get("rubric") or {}),
                excluded_by=list(reasons.get("excluded_by") or []),
            )
        )
    return feed


@router.get("/summary", response_model=MatchSummaryOut)
async def summary(session: SessionDep, profile_id: uuid.UUID | None = None) -> MatchSummaryOut:
    """How many matches exist, and how many are still unrated.

    A separate route because `GET /matches` is a *page* — it caps at 200 and
    defaults to 50. The dashboard read that page length as a total and
    reported "50 matches" against a database holding 1,853. A count has to
    come from a count.
    """
    scoped = select(func.count()).select_from(Match)
    if profile_id is not None:
        scoped = scoped.where(Match.profile_id == profile_id)

    total = await session.scalar(scoped) or 0
    undecided = await session.scalar(scoped.where(Match.decision.is_(None))) or 0
    interested = await session.scalar(scoped.where(Match.decision == "interested")) or 0

    return MatchSummaryOut(total=total, undecided=undecided, interested=interested)


DECISIONS = ("interested", "skipped")


@router.post("/{match_id}/decision", response_model=MatchDecisionOut)
async def decide(match_id: uuid.UUID, body: MatchDecision, session: SessionDep) -> Match:
    """Record what the owner thinks of a posting.

    Right is `interested`, left is `skipped`, and neither applies to anything
    — this is a verdict on the posting, not an instruction to the worker.
    Keeping it that way is what makes a fast feed safe to use: §2.3 says
    nothing submits without explicit approval, and a swipe is not that.

    Re-deciding is allowed and overwrites. A feed you cannot correct is one
    you use carefully and slowly, which defeats the point.
    """
    match = await session.get(Match, match_id)
    if match is None:
        raise ApiError(ErrorCode.NOT_FOUND, "match not found")

    if body.decision not in DECISIONS:
        raise ApiError(ErrorCode.INVALID_REQUEST, f"decision must be one of {', '.join(DECISIONS)}")

    match.decision = body.decision
    match.decided_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(match)
    return match


@router.get("/calibration", response_model=CalibrationOut)
async def calibration(session: SessionDep, profile_id: uuid.UUID | None = None) -> CalibrationOut:
    """What the owner's swipes say the threshold should be.

    This is the point of recording decisions. `min_match_score` ships at 0.75,
    and the first real scoring run over 10,922 postings produced a **maximum**
    of 0.271 — a threshold the metric cannot reach, so nothing would ever
    clear it. A number that cannot be met is not a safety setting, it is a
    silent off switch.

    Rather than picking a new constant by eye, this reads it off the swipes:
    the suggested threshold is the score below which the owner has skipped
    almost everything. Until there are enough decisions to say that, it
    returns null and says so — §15's whole complaint about Gate 5 is that a
    number derived from fixtures answers a question nobody asked.
    """
    query = select(Match).where(Match.decision.is_not(None))
    if profile_id is not None:
        query = query.where(Match.profile_id == profile_id)
    decided = list((await session.scalars(query)).all())

    interested = sorted(m.score for m in decided if m.decision == "interested")
    skipped = [m.score for m in decided if m.decision == "skipped"]

    suggested: float | None = None
    separation: float | None = None
    if len(interested) >= MIN_DECISIONS_TO_SUGGEST and skipped:
        # The 10th percentile of what was kept: low enough to admit most of
        # what the owner wants, and derived rather than chosen.
        index = max(int(len(interested) * 0.1) - 1, 0)
        suggested = round(interested[index], 3)
        separation = round((sum(interested) / len(interested)) - (sum(skipped) / len(skipped)), 3)

    return CalibrationOut(
        decided=len(decided),
        interested=len(interested),
        skipped=len(skipped),
        interested_mean=round(sum(interested) / len(interested), 3) if interested else None,
        skipped_mean=round(sum(skipped) / len(skipped), 3) if skipped else None,
        #: Positive means the scorer ranks what the owner keeps above what they
        #: discard. Zero or negative means it is not measuring what they want,
        #: which no threshold can fix.
        separation=separation,
        suggested_min_score=suggested,
        enough_data=suggested is not None,
    )
