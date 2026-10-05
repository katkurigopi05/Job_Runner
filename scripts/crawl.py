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

**Two sweeps, and the second one had no caller either.** Without `--dispatch`
this enqueues the original in-process cycle, which reads `seeds/companies.yaml`
and polls each board in one long task. `--dispatch` enqueues the per-company
sweep in `packages/crawler/dispatch.py`, which reads the `companies` **table** —
so it is the only path that reaches an imported CSV, and the only one that
dispatches discovery for a company whose board is not known yet.

Nothing enqueued that. `handle_crawl` has handled `dispatch` since the sweep was
written, and the only producer of such a task was the tick scheduling its own
successor — which cannot happen until something schedules a first one. The same
shape as the defect in the paragraph above, one layer in: a whole sweep that
could run and was never asked.

The default is left alone deliberately. `make crawl` has meant "poll the curated
registry" for as long as it has existed, and quietly repointing it at a
thousands-row candidate table would change what one command costs without
saying so.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

from apps.worker.crawl_job import request_crawl
from packages.core import db as core_db


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1 — a backlog of 0 dispatches nothing")
    return number


def payload_from(argv: list[str] | None = None) -> dict[str, Any]:
    """Parse `make crawl`'s flags into the payload `handle_crawl` reads."""
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
    parser.add_argument(
        "--dispatch",
        action="store_true",
        help=(
            "Sweep the companies table rather than the seed file: one task per "
            "company, discovery for those without a verified board and a fetch for "
            "those with one. This is the path an imported CSV is reachable by."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Companies to enqueue in this tick. Requires --dispatch.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help=(
            "Do not schedule the next tick. A dispatch tick normally re-arms "
            "itself, which is what makes the sweep continuous — and what makes an "
            "unattended pilot keep going after you stop watching."
        ),
    )
    parser.add_argument(
        "--max-backlog",
        type=_positive,
        default=None,
        help=(
            "Outstanding company tasks a tick may leave queued before it stops "
            "dispatching. Defaults to CRAWLER_MAX_BACKLOG. Raise it to queue a "
            "whole imported sheet at once. Requires --dispatch."
        ),
    )
    args = parser.parse_args(argv)

    if (args.limit is not None or args.once or args.max_backlog is not None) and not args.dispatch:
        # All three only mean anything to the dispatching path, and silently
        # accepting them on the other one is how a bounded pilot turns out to
        # have been unbounded.
        parser.error("--limit, --once and --max-backlog require --dispatch")

    payload: dict[str, Any] = {}
    if args.seed_path:
        payload["seed_path"] = args.seed_path
    if args.force:
        payload["force"] = True
    if args.dispatch:
        payload["dispatch"] = True
    if args.limit is not None:
        payload["limit"] = args.limit
    if args.once:
        payload["repeat"] = False
    if args.max_backlog is not None:
        payload["max_backlog"] = args.max_backlog
    return payload


async def main() -> None:
    payload = payload_from()
    async with core_db.get_sessionmaker()() as session:
        # The guard lives in `request_crawl`, shared with the assistant's
        # "run crawler", so the two cannot disagree about when a crawl waits.
        requested = await request_crawl(session, payload)
        if requested.queued is None:
            print(
                f"{requested.waiting} crawl task(s) already pending or running — "
                "not adding another."
            )
            print("Start the worker with `make worker` if nothing is draining them.")
            return
        await session.commit()

    print(f"queued crawl task {requested.queued.id}")
    print("Run `make worker` if it is not already running — nothing happens until it drains.")


if __name__ == "__main__":
    asyncio.run(main())
