"""Enqueue one registry crawl — `make crawl`.

`apps/worker/crawl_job.py` has existed and been wired into the worker's handler
map since Phase 5, and nothing has ever enqueued it. The queue in this database
holds 70 `apply` tasks and not one `crawl`, so `handle_crawl` has never run
through the worker at all: the registry's companies are polled only if someone
inserts a row by hand, which nothing documented how to do.

That is why postings go stale without anything looking wrong. The crawler is
not broken and not slow — it is simply never asked, and "no new postings since
the last sweep" reads identically to "the sweep never happened".

Enqueues rather than crawling in-process on purpose. The worker owns the browser
and the rate limiter, and a second crawler running beside it would poll the same
hosts on a counter the worker cannot see — §2.6's floors are per host, and two
processes each honouring them independently honour neither.

Needs `make worker` running to do anything. That is stated by the script rather
than assumed, because an enqueue that silently sits in a queue nobody is
draining is the same failure in a new place.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

from sqlalchemy import func, select

from apps.worker.crawl_job import CRAWL_TASK_KIND
from packages.core import db as core_db
from packages.core.enums import QueueTaskStatus
from packages.core.models import QueueTask
from packages.core.queue import enqueue


async def main() -> None:
    parser = argparse.ArgumentParser(description="Queue one crawl of the company registry.")
    parser.add_argument(
        "--seed-path",
        default=None,
        help="Seed file to crawl. Defaults to the registry the crawler already uses.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Re-emit postings whose content hash is unchanged. Normally a second run "
            "emits nothing, which is change detection working — use this only when "
            "you have a reason to distrust the stored hashes."
        ),
    )
    args = parser.parse_args()

    payload: dict[str, Any] = {}
    if args.seed_path:
        payload["seed_path"] = args.seed_path
    if args.force:
        payload["force"] = True

    unfinished = (QueueTaskStatus.PENDING.value, QueueTaskStatus.RUNNING.value)

    async with core_db.get_sessionmaker()() as session:
        # A crawl already waiting makes a second one pointless: they would poll
        # the same hosts minutes apart and the later one would emit nothing.
        pending = await session.scalar(
            select(func.count())
            .select_from(QueueTask)
            .where(QueueTask.kind == CRAWL_TASK_KIND, QueueTask.status.in_(unfinished))
        )
        if pending:
            print(f"{pending} crawl task(s) already pending or running — not adding another.")
            print("Start the worker with `make worker` if nothing is draining them.")
            return

        task = await enqueue(session, CRAWL_TASK_KIND, payload)
        await session.commit()

    print(f"queued crawl task {task.id}")
    print("Run `make worker` if it is not already running — nothing happens until it drains.")


if __name__ == "__main__":
    asyncio.run(main())
