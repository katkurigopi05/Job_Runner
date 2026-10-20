"""How much crawl work is left, and how long it will take.

Asked for by the owner on 2026-10-19, who otherwise had to ask: the spider
said "crawling" and nothing about whether that meant minutes or a night. For
each stage — polling known boards, discovering companies' boards, the deep
dive into the ones discovery could not read — this counts what is left, how
fast it went over the last half hour, and what that rate implies.

The rate is the evidence, not a promise: a stage whose tasks wait 60 seconds
per site runs at the pace its hosts allow, and a stage behind another one
does not move until that one is done, which is said rather than estimated.
Read-only, and cheap enough for the spider's ten-second poll: one aggregate
over unfinished tasks and the last half hour's finished ones.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.crawler.dispatch import CRAWL_COMPANY_TASK_KIND, DISCOVER_COMPANY_TASK_KIND
from packages.crawler.issues import DEEP_DIVE_TASK_KIND

#: The stages, in the order the queue serves them (`run.YIELDING_KINDS`): a
#: later one waits while an earlier one has work ready.
STAGES: tuple[tuple[str, str], ...] = (
    ("boards", CRAWL_COMPANY_TASK_KIND),
    ("discovery", DISCOVER_COMPANY_TASK_KIND),
    ("deep dives", DEEP_DIVE_TASK_KIND),
)

#: How far back the rate is measured. Long enough to smooth a host's 60s
#: waits, short enough to follow a change of pace within the hour.
RATE_WINDOW = timedelta(minutes=30)


@dataclass(frozen=True)
class StageProgress:
    """One stage's backlog, pace and estimate."""

    stage: str
    kind: str
    #: Tasks that could run now.
    waiting: int
    #: Tasks due later: a retry, or one handed back for a busy host.
    scheduled: int
    running: int
    #: Distinct companies (or notes, for the deep dive) those tasks are for.
    #: Fewer than the tasks when one was queued twice.
    items: int
    #: Finished in the last `RATE_WINDOW`.
    done_recently: int
    per_minute: float
    #: Minutes until the tasks left are done at that rate. None when the
    #: stage is not moving, so no rate has been measured.
    eta_minutes: float | None
    #: The earlier stage this one is waiting behind, if it is.
    after: str | None


async def crawl_backlog(
    session: AsyncSession, *, window: timedelta = RATE_WINDOW
) -> list[StageProgress]:
    """Every stage, in serving order, whether or not it has work."""
    kinds = [kind for _, kind in STAGES]
    rows = (
        await session.execute(
            text("""
            SELECT kind,
              count(*) FILTER (WHERE status = 'pending' AND run_after <= now()) AS waiting,
              count(*) FILTER (WHERE status = 'pending' AND run_after > now()) AS scheduled,
              count(*) FILTER (WHERE status = 'running') AS running,
              count(DISTINCT coalesce(payload_json->>'company_id', payload_json->>'issue_id'))
                FILTER (WHERE status IN ('pending', 'running')) AS items,
              count(*) FILTER (
                WHERE status = 'done'
                  AND finished_at >= now() - make_interval(secs => :window_s)
              ) AS done_recently
            FROM queue_tasks
            WHERE kind = ANY(:kinds)
              AND (status IN ('pending', 'running')
                   OR finished_at >= now() - make_interval(secs => :window_s))
            GROUP BY kind
            """),
            {"kinds": kinds, "window_s": window.total_seconds()},
        )
    ).all()
    by_kind = {row.kind: row for row in rows}
    minutes = window.total_seconds() / 60

    progress: list[StageProgress] = []
    ahead_with_work: str | None = None
    for stage, kind in STAGES:
        row = by_kind.get(kind)
        waiting = int(row.waiting) if row else 0
        scheduled = int(row.scheduled) if row else 0
        running = int(row.running) if row else 0
        done = int(row.done_recently) if row else 0
        per_minute = round(done / minutes, 2)
        left = waiting + scheduled + running
        progress.append(
            StageProgress(
                stage=stage,
                kind=kind,
                waiting=waiting,
                scheduled=scheduled,
                running=running,
                items=int(row.items) if row else 0,
                done_recently=done,
                per_minute=per_minute,
                eta_minutes=round(left / per_minute, 1) if left and per_minute else None,
                after=ahead_with_work if left and not per_minute else None,
            )
        )
        # Boards are served first but finish in seconds; only a stage with a
        # real queue holds the next one back long enough to be worth saying.
        if waiting and stage != "boards":
            ahead_with_work = stage
    return progress


__all__ = ["RATE_WINDOW", "STAGES", "StageProgress", "crawl_backlog"]
