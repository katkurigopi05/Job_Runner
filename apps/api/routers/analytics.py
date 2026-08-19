"""Reports over the project's own history.

The scoring, applying, and inbox paths have been writing the rows these read
since Phase 0, and nothing ever asked them a question. That is the gap
`docs/REFERENCE.md` §3.5 named: a scorer nobody checks is a scorer that can
drift for months while every dashboard stays green.

Read-only, all of it. None of these routes writes, enqueues, or transitions
anything — a report that could change an application's state would be a
report the owner has to be careful about running.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from apps.api.deps import SessionDep
from packages.analytics import cadence, digest, funnel
from packages.core.schemas import CadenceOut, DigestOut, FunnelOut, LatencyOut, SilentOut

router = APIRouter(prefix="/analytics", tags=["analytics"])

MAX_WINDOW_DAYS = 365


def _funnel_out(report: funnel.FunnelReport) -> FunnelOut:
    return FunnelOut(
        total=report.stages.total,
        submitted=report.stages.submitted,
        needs_review=report.stages.needs_review,
        failed=report.stages.failed,
        answered=report.stages.answered,
        engaged=report.stages.engaged,
        answer_rate=report.stages.answer_rate,
        engagement_rate=report.stages.engagement_rate,
        unscored=report.unscored,
        score_tracks_outcome=report.score_tracks_outcome,
        buckets=[
            {
                "label": bucket.label,
                "applications": bucket.applications,
                "submitted": bucket.submitted,
                "answered": bucket.answered,
                "engaged": bucket.engaged,
                "engagement_rate": bucket.engagement_rate,
                # Surfaced so the UI can refuse to draw a rate the sample
                # does not support, rather than drawing it faintly.
                "is_meaningful": bucket.is_meaningful,
            }
            for bucket in report.buckets
        ],
    )


def _latency_out(report: cadence.LatencyReport) -> LatencyOut:
    return LatencyOut(
        samples=report.samples,
        median_days=report.median_days,
        fastest_days=report.fastest_days,
        slowest_days=report.slowest_days,
        median_rejection_days=report.median_rejection_days,
        median_engagement_days=report.median_engagement_days,
        suggested_silent_after_days=report.suggested_silent_after_days,
    )


@router.get("/funnel", response_model=FunnelOut)
async def read_funnel(session: SessionDep, profile_id: str | None = None) -> FunnelOut:
    """Where applications stop, and whether a higher score fares better.

    `score_tracks_outcome` is null until at least two score bands hold enough
    applications to read a rate from. That is the honest answer for most of
    this tool's life, and much better than a verdict drawn from four
    applications.
    """
    return _funnel_out(await funnel.build(session, profile_id=profile_id))


@router.get("/cadence", response_model=CadenceOut)
async def read_cadence(
    session: SessionDep,
    profile_id: str | None = None,
    silent_after_days: int = Query(default=cadence.DEFAULT_SILENT_AFTER_DAYS, ge=1, le=180),
) -> CadenceOut:
    """Submitted applications the employer has not answered, oldest first.

    Reports only. Nothing here sends a follow-up: §2 gives this project no
    permission to correspond with an employer on its own, and that is the
    owner's message to write.
    """
    report = await cadence.silence(
        session, silent_after_days=silent_after_days, profile_id=profile_id
    )
    latency = await cadence.latency(session, profile_id=profile_id)
    return CadenceOut(
        silent=[
            SilentOut(
                application_id=item.application_id,
                url=item.url,
                days_since=item.days_since,
                stale=item.stale,
            )
            for item in report.silent
        ],
        due=len(report.due),
        stale=len(report.stale),
        latency=_latency_out(latency),
    )


@router.get("/digest", response_model=DigestOut)
async def read_digest(
    session: SessionDep,
    profile_id: str | None = None,
    window_days: int = Query(default=digest.DEFAULT_WINDOW_DAYS, ge=1, le=MAX_WINDOW_DAYS),
) -> DigestOut:
    """A week in one object."""
    report = await digest.build(session, window_days=window_days, profile_id=profile_id)
    return DigestOut(
        window_days=report.window_days,
        postings_seen=report.postings_seen,
        applications_created=report.applications_created,
        applications_submitted=report.applications_submitted,
        replies_received=report.replies_received,
        awaiting_review=report.awaiting_review,
        follow_ups_due=report.follow_ups_due,
        quiet_week=report.quiet_week,
        funnel=_funnel_out(report.funnel),
        latency=_latency_out(report.latency),
    )
