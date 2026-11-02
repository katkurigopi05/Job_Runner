"""Read every missing job description now — `make backfill-descriptions`.

The reading is `packages/crawler/backfill.py`: one request a minute per host,
robots.txt honoured, each posting committed as it lands. Afterwards the
matching pass embeds and scores what was filled and the chunker makes it
searchable, unless `--no-index`. That pass is the one a crawl runs, so with
`POSTING_AUTO_PRUNE` on it also prunes, as every crawl does.

    make backfill-descriptions dry=1     # what is missing, per host, and how long
    make backfill-descriptions limit=3   # three pages a host, to see it work
    make backfill-descriptions           # all of it
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from packages.core import db as core_db
from packages.core.config import get_settings
from packages.crawler.backfill import (
    MAX_HOSTS,
    READERS,
    HostReport,
    Pace,
    PacedRateLimiter,
    backfill,
    find_pending,
)
from packages.crawler.fetch import PoliteFetcher, build_fetcher

#: Matching passes before giving up on a backlog that will not shrink.
MAX_PASSES = 50
#: Progress is printed after this many pages, across all hosts.
PRINT_EVERY = 25


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="count per host and stop")
    parser.add_argument(
        "--ats", default=",".join(READERS), help="boards to read (default: %(default)s)"
    )
    parser.add_argument("--limit", type=int, default=None, help="at most N pages per host")
    parser.add_argument(
        "--max-hosts",
        type=int,
        default=MAX_HOSTS,
        help="hosts read at once (default: %(default)s). Each still waits its own "
        "minute between pages, so more hosts finish the small boards sooner; the "
        "largest board decides when the run ends.",
    )
    parser.add_argument(
        "--left-waiting",
        type=Path,
        default=None,
        help="pages earlier runs were refused or could not read, read after the rest "
        "and added to as this run goes (default: <storage_root>/logs/"
        "backfill_left_waiting.txt)",
    )
    parser.add_argument(
        "--pace",
        default=None,
        help="seconds between job pages on one host, slowest last, e.g. 5,10,15,20: "
        "steps up when many pages are refused and stops past the last step. "
        "§2.6 amended for this backfill only (2026-11-02); without it, 60 s",
    )
    parser.add_argument(
        "--no-retry",
        action="store_true",
        help="leave the pages in the left-waiting file unread: they stay without text",
    )
    parser.add_argument(
        "--no-index", action="store_true", help="skip embedding, scoring and chunking"
    )
    return parser.parse_args(argv)


def _pace(text: str | None) -> Pace | None:
    if not text:
        return None
    return Pace(steps=tuple(float(step) for step in text.split(",") if step.strip()))


def _paced_fetcher(pace: Pace) -> PoliteFetcher:
    """The crawler's fetcher, spaced by the run's pace instead of 60 s."""
    settings = get_settings()
    return PoliteFetcher(
        rate_limiter=PacedRateLimiter(pace=pace),
        max_connections=settings.crawler_http_max_connections,
        max_keepalive=settings.crawler_http_max_keepalive,
        timeout=settings.crawler_http_timeout_s,
        request_budget=settings.crawler_request_budget,
        budget_window_seconds=settings.crawler_budget_window_seconds,
    )


async def dry_run(ats: list[str]) -> int:
    async with core_db.get_sessionmaker()() as session:
        pending = await find_pending(session, ats=ats)
    sizes = sorted(((len(items), host) for host, items in pending.items()), reverse=True)
    total = sum(n for n, _ in sizes)
    print(f"{total} open postings with no description, on {len(sizes)} hosts")
    for n, host in sizes[:15]:
        print(f"  {n:6d}  {host}")
    if sizes:
        hours = sizes[0][0] * get_settings().crawler_min_delay_s / 3600
        print(f"one request a minute per host: the largest decides, at least {hours:.1f} hours")
    return 0


async def index() -> None:
    """Embed, score and chunk what was filled, as the crawler's pass would."""
    from packages.matching.incremental import run_matching_pass
    from scripts.chunk_postings import run as chunk_all

    sessions = core_db.get_sessionmaker()
    for _ in range(MAX_PASSES):
        async with sessions() as session:
            report = await run_matching_pass(session)
            await session.commit()
        print(f"  matching pass: {report.summary()}", flush=True)
        if not (report.embedded or report.scored):
            break
    await chunk_all()


async def run(argv: list[str] | None = None) -> int:
    args = _args(argv)
    ats = [kind.strip() for kind in args.ats.split(",") if kind.strip()]
    if args.dry_run:
        return await dry_run(ats)

    started = time.monotonic()
    seen: dict[str, HostReport] = {}
    printed = 0

    def progress(report: HostReport) -> None:
        nonlocal printed
        seen[report.host] = report
        filled = sum(r.filled for r in seen.values())
        failed = sum(r.failed for r in seen.values())
        if filled + failed - printed >= PRINT_EVERY:
            printed = filled + failed
            minutes = (time.monotonic() - started) / 60
            print(f"  {filled} filled, {failed} left waiting, {minutes:.0f} min", flush=True)

    waiting_file = args.left_waiting or (
        Path(get_settings().storage_root) / "logs" / "backfill_left_waiting.txt"
    )
    tried = frozenset(waiting_file.read_text().split()) if waiting_file.exists() else frozenset()
    if tried:
        what = "left without text" if args.no_retry else "read after the rest"
        print(f"{len(tried)} pages left waiting by earlier runs are {what}")
    waiting_file.parent.mkdir(parents=True, exist_ok=True)
    with waiting_file.open("a") as sink:

        def left_waiting(external_id: str) -> None:
            sink.write(external_id + "\n")
            sink.flush()

        pace = _pace(args.pace)
        if pace is not None:
            print(f"pace: {', '.join(f'{step:g}' for step in pace.steps)} s a host")
        async with _paced_fetcher(pace) if pace else build_fetcher(shared=True) as fetcher:
            report = await backfill(
                fetcher,
                core_db.get_sessionmaker(),
                ats=ats,
                limit=args.limit,
                max_hosts=args.max_hosts,
                tried_before=tried,
                retry_tried=not args.no_retry,
                on_progress=progress,
                on_left_waiting=left_waiting,
                pace=pace,
            )
    minutes = (time.monotonic() - started) / 60
    print(
        f"filled {report.filled}, left {report.failed} waiting, skipped {report.skipped} "
        f"closed or filled meanwhile, on {len(report.hosts)} hosts in {minutes:.0f} min"
    )
    if report.blocked_hosts:
        print(f"robots.txt refused job pages on: {', '.join(report.blocked_hosts)}")
    if report.refused_hosts:
        print(f"left alone after refusing pages: {', '.join(report.refused_hosts)}")
    if pace is not None:
        print(
            f"pace ended at {pace.current:g} s a host"
            + (": stopped, the sites were refusing at the slowest step" if pace.stopped else "")
        )
    if report.filled and not args.no_index:
        await index()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
