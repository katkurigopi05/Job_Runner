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

from packages.core import db as core_db
from packages.core.config import get_settings
from packages.crawler.backfill import MAX_HOSTS, READERS, HostReport, backfill, find_pending
from packages.crawler.fetch import build_fetcher

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
        "--no-index", action="store_true", help="skip embedding, scoring and chunking"
    )
    return parser.parse_args(argv)


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

    async with build_fetcher(shared=True) as fetcher:
        report = await backfill(
            fetcher,
            core_db.get_sessionmaker(),
            ats=ats,
            limit=args.limit,
            max_hosts=args.max_hosts,
            on_progress=progress,
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
    if report.filled and not args.no_index:
        await index()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
