"""Crawler status — is it working right now, and when did it last finish.

The queue was invisible from the dashboard. `make crawl` enqueues, `make worker`
drains, and nothing on any screen said whether either was happening — so a stale
match feed looked identical to a fresh one with nothing new, and telling them
apart meant opening a terminal and querying Postgres.

That is the blindness `/health` had before it learned to fail: the answer
existed and nothing surfaced it.

Read-only on purpose. This said "starting a crawl stays a deliberate act at a
terminal, because it makes real outbound requests to employers' sites", and
the owner reversed the terminal half on 2026-10-04: typing "run crawler" or
"find new jobs" to the assistant now starts one (`chat.asks_to_crawl`). The
reason survives intact. It is still a deliberate act — a typed command, matched
in code, never inferred from a question — still reachable only from this
machine, and it still goes through `crawl_job.request_crawl`, so robots.txt,
the per-host floors and the one-crawl-at-a-time guard are unchanged. This
router stays read-only.

**That last sentence is reversed too**, on 2026-10-08. The owner asked for the
MCP server to be able to start a crawl, and its tools go through HTTP on
purpose (`apps/mcp/client.py`), so there has to be a route to ask. `POST
/crawl` is that and nothing more: `request_crawl`, a commit, and whether a
worker is alive to run it. What keeps it deliberate over MCP is on that side:
the tool is one of the ones that make the client ask the owner on every call.
"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import and_, desc, func, not_, or_, select

from apps.api.deps import SessionDep
from apps.worker.crawl_job import CRAWL_TASK_KIND, request_crawl
from packages.core import heartbeat
from packages.core.enums import QueueTaskStatus
from packages.core.heartbeat import WorkerState
from packages.core.models import Posting, QueueTask
from packages.core.schemas import CrawlStartOut, CrawlStatusOut
from packages.crawler.dispatch import CRAWL_COMPANY_TASK_KIND, DISCOVER_COMPANY_TASK_KIND

router = APIRouter(prefix="/crawl", tags=["crawl"])


@router.get("/status", response_model=CrawlStatusOut)
async def crawl_status(session: SessionDep) -> CrawlStatusOut:
    """Follow the whole crawl, including company tasks and their rate-limit waits."""
    # One snapshot: separate counts can miss work while a pending company is
    # claimed between queries. A dispatch task finishing is only a handoff,
    # not the end of the crawl it queued.
    is_running = QueueTask.status == QueueTaskStatus.RUNNING.value
    # A claim counts as held only while its lease does. A worker killed
    # mid-task leaves the row `running` until a claimant takes the lease
    # back, and with none alive nothing does: counted as held, the spider
    # walked for good. Past its lease it is waiting work like any other.
    # The same test `queue.claim_task` reclaims by, so the two cannot differ.
    lease_lapsed = and_(
        is_running,
        QueueTask.lease_expires_at.is_not(None),
        QueueTask.lease_expires_at < func.now(),
    )
    lease_held = and_(is_running, not_(lease_lapsed))
    unclaimed = or_(
        lease_lapsed,
        and_(
            QueueTask.status == QueueTaskStatus.PENDING.value,
            or_(
                QueueTask.kind != CRAWL_TASK_KIND,
                QueueTask.run_after <= func.now(),
                # A retry belongs to unfinished work, unlike a future tick.
                QueueTask.attempts > 0,
            ),
        ),
    )
    held, pending = (
        await session.execute(
            select(
                func.count().filter(lease_held),
                func.count().filter(unclaimed),
            ).where(
                QueueTask.kind.in_(
                    (CRAWL_TASK_KIND, CRAWL_COMPANY_TASK_KIND, DISCOVER_COMPANY_TASK_KIND)
                )
            )
        )
    ).one()
    running = bool(held)
    if pending and not running:
        # Keep moving between claims and while polite fetching is deferred,
        # but never animate a queue with nobody alive to drain it.
        running = any(
            worker.state is WorkerState.ALIVE for worker in await heartbeat.workers(session)
        )

    last = (
        await session.scalars(
            select(QueueTask)
            .where(
                QueueTask.kind == CRAWL_TASK_KIND,
                QueueTask.status.in_((QueueTaskStatus.DONE.value, QueueTaskStatus.FAILED.value)),
            )
            # `finished_at` since 2026-10-19; a tick finished before then has
            # only `locked_at`, when the worker claimed it.
            .order_by(desc(func.coalesce(QueueTask.finished_at, QueueTask.locked_at)))
            .limit(1)
        )
    ).first()

    return CrawlStatusOut(
        running=bool(running),
        pending=int(pending or 0),
        # An unclaimed queue with no live workers is not a completed crawl.
        stalled=bool(pending) and not bool(running),
        last_finished_at=(last.finished_at or last.locked_at) if last else None,
        last_status=last.status if last else None,
        # The number that actually answers "are my postings current". A crawl
        # that ran and found nothing leaves this unchanged, which is the truth.
        newest_posting_at=await session.scalar(select(func.max(Posting.first_seen_at))),
    )


@router.post("", response_model=CrawlStartOut)
async def start_crawl(session: SessionDep) -> CrawlStartOut:
    """Queue one registry crawl, unless one is already waiting or running."""
    requested = await request_crawl(session, trigger="manual")
    await session.commit()
    return CrawlStartOut(
        queued=requested.queued is not None,
        already_waiting=requested.waiting,
        worker_alive=any(
            worker.state is WorkerState.ALIVE for worker in await heartbeat.workers(session)
        ),
    )
