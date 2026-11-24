"""Hand a task back when its host is busy, instead of sleeping on it.

`apps/worker/run.py::run_once` claims one task and awaits the handler to
completion, and `claim_task` orders `ORDER BY run_after`. So a handler that
sleeps inside `rate_limiter.acquire()` holds its worker slot for the whole wait:
with four workers and four tasks for one throttled host, every slot is asleep
while a task for an idle host sits pending behind them. The per-host counters
were always separate — it is the *pool* that was the bottleneck, not the limiter.

Deferring happens **before any request is made**, which is what makes it safe:

- nothing successful is replayed, because nothing has been sent yet;
- no discovery result or page of a paginated board is lost, because none has
  been fetched;
- no slot is reserved, so the limiter is not charged for a request that did not
  happen — the peek reads the marker and takes nothing.

It is a scheduling hint and never the enforcement. `acquire` still reserves
atomically at request time; between this peek and that reservation another
worker may take the slot, and that is fine — it will be told to wait.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.enums import QueueTaskStatus
from packages.core.models import Company, QueueTask
from packages.core.queue import ClaimedTask, ClaimOwnershipLost, TaskDeferred, defer_task
from packages.crawler.fetch import PoliteFetcher
from packages.crawler.ratelimit import budget_key, host_key

log = structlog.get_logger(__name__)

#: A wait worth giving the slot back for. Below this, sleeping is cheaper than a
#: database round trip and a re-claim — and on a shared ATS host the ordinary
#: floor is 2s, so a threshold much lower than this would defer almost every
#: task almost every time and turn the queue into a spin.
DEFAULT_THRESHOLD_S = 3.0

#: How many times one task may be deferred before it waits instead.
#:
#: Without a bound a task on a host that is always busy could be handed back for
#: ever: each deferral returns it at the moment the host frees, where another
#: worker may already be reserving. That is not a deadlock — someone always
#: progresses — but it could starve one company indefinitely, so after this many
#: deferrals the task takes the slot and waits however long it must.
MAX_DEFERRALS = 5


async def defer_if_host_busy(
    session: AsyncSession,
    claimed: ClaimedTask,
    fetcher: PoliteFetcher,
    url: str,
    *,
    threshold_s: float | None = None,
    max_deferrals: int | None = None,
    now: datetime | None = None,
) -> None:
    """Hand the task back if `url`'s host is busy. Does not commit.

    Raises `TaskDeferred` when it does, because a handler that merely returned
    would be indistinguishable from one that succeeded and `_process` would mark
    the task `done` — overwriting the deferral it had just written.

    The bounds are read here rather than bound as default arguments so a test
    can change them; a default argument is evaluated once at import and
    monkeypatching the module constant then does nothing, which is how the
    control arm of `test_worker_host_blocking` first failed to disable this.
    """
    limiter = fetcher.rate_limiter
    if limiter is None:  # pragma: no cover - build_fetcher always sets one
        return

    threshold = DEFAULT_THRESHOLD_S if threshold_s is None else threshold_s
    bound = MAX_DEFERRALS if max_deferrals is None else max_deferrals

    host = budget_key(host_key(url))
    payload = dict(claimed.task.payload_json or {})
    deferrals = int(payload.get("deferrals", 0))
    if deferrals >= bound:
        log.info("host_busy_waiting_rather_than_deferring", host=host, deferrals=deferrals)
        return

    waiting = await limiter.wait_for(host)
    if waiting < threshold:
        return

    payload["deferrals"] = deferrals + 1
    claimed.task.payload_json = payload
    run_after = (now or datetime.now(UTC)) + timedelta(seconds=waiting)
    if not await defer_task(session, claimed, run_after=run_after):
        raise ClaimOwnershipLost(str(claimed.task_id))
    log.info(
        "task_deferred_host_busy",
        kind=claimed.task.kind,
        host=host,
        waiting_s=round(waiting, 1),
        deferrals=deferrals + 1,
    )
    raise TaskDeferred(f"{host} busy for {waiting:.1f}s", run_after=run_after)


#: Boards of one zone (`ratelimit.SHARED_ZONES`) read at once.
#:
#: A zone is one counter, so every board in it shares one request every few
#: seconds, and `defer_if_host_busy` stops handing a task back after
#: `MAX_DEFERRALS`. Without this bound each free worker would end up holding a
#: Workday board, every other company would wait behind them, and with five
#: boards interleaved a 100-page board takes longer than its task's time limit.
#: Two keeps the largest board inside that limit.
ZONE_BOARDS_AT_ONCE = 2

#: How long a board waiting for its zone is handed back for. A Workday board
#: takes a few minutes to read, so looking more often only spins the queue.
ZONE_RECHECK_S = 120.0


async def defer_if_zone_full(
    session: AsyncSession,
    claimed: ClaimedTask,
    url: str,
    *,
    ats_type: str,
    now: datetime | None = None,
) -> None:
    """Hand the task back while `ZONE_BOARDS_AT_ONCE` boards of its zone are read.

    Raises `TaskDeferred` when it does, as `defer_if_host_busy` does. A zone's
    boards are one ATS's, so the boards being read are this kind's running
    tasks for that ATS, with a live lease: a worker that died holds no place.

    Not counted against `MAX_DEFERRALS`. That bound protects one company from
    being outrun on a busy host; here the boards ahead of it finish, so it
    always gets its turn. Like the busy check it is a scheduling hint: two
    tasks claimed in the same instant can both go ahead, and the zone's one
    counter still paces every request they make.
    """
    if budget_key(host_key(url)) == host_key(url):
        return
    current = now or datetime.now(UTC)
    reading = await session.scalar(
        select(func.count())
        .select_from(QueueTask)
        .join(Company, cast(Company.id, String) == QueueTask.payload_json["company_id"].astext)
        .where(
            QueueTask.kind == claimed.task.kind,
            QueueTask.status == QueueTaskStatus.RUNNING.value,
            QueueTask.lease_expires_at > current,
            QueueTask.id != claimed.task_id,
            Company.ats_type == ats_type,
        )
    )
    if (reading or 0) < ZONE_BOARDS_AT_ONCE:
        return
    run_after = current + timedelta(seconds=ZONE_RECHECK_S)
    if not await defer_task(session, claimed, run_after=run_after):
        raise ClaimOwnershipLost(str(claimed.task_id))
    log.info("task_deferred_zone_full", ats=ats_type, reading=reading)
    raise TaskDeferred(f"{ats_type}: {reading} boards being read", run_after=run_after)
