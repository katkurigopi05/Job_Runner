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
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.queue import ClaimedTask, TaskDeferred, defer_task
from packages.crawler.fetch import PoliteFetcher
from packages.crawler.ratelimit import host_key

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

    host = host_key(url)
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
    await defer_task(session, claimed.task, run_after=run_after)
    log.info(
        "task_deferred_host_busy",
        kind=claimed.task.kind,
        host=host,
        waiting_s=round(waiting, 1),
        deferrals=deferrals + 1,
    )
    raise TaskDeferred(f"{host} busy for {waiting:.1f}s", run_after=run_after)
