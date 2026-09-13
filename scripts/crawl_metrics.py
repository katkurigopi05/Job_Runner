"""Where the crawl's time went — `make crawl-metrics`.

    make crawl-metrics
    make crawl-metrics n=30

Answers one question with measurement instead of arithmetic: **is this slow
because of a rule we chose, or because of the network?** Those have opposite
responses. Time inside the rate limiter is §2.6 being obeyed and driving it
down means breaking it; time in the request is the only half where a speed-up
is available at all.

Reads the counters `crawler/meter.py` keeps on each host row. Nothing here
fetches, so it is safe to run mid-crawl and costs nothing.
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from packages.core import db as core_db
from packages.core.models import CrawlerHostBudget
from packages.crawler.ratelimit import SHARED_API_HOSTS


def _row(host: str, requests: int, waited: float, network: float, shared: bool) -> str:
    per = network / requests if requests else 0.0
    share = waited / (waited + network) * 100 if (waited + network) else 0.0
    floor = "2s" if shared else "60s"
    return (
        f"  {host[:38]:<38} {requests:>6}  {waited:>9.1f}s {network:>9.1f}s"
        f" {per * 1000:>8.0f}ms {share:>5.0f}%  {floor}"
    )


async def run(limit: int) -> str:
    async with core_db.get_sessionmaker()() as session:
        rows = list(
            (
                await session.scalars(
                    select(CrawlerHostBudget).order_by(CrawlerHostBudget.requests.desc())
                )
            ).all()
        )

    counted = [r for r in rows if r.requests]
    if not counted:
        return (
            "No requests counted yet.\n"
            "  Counters start at the next fetch — an existing host row has served\n"
            "  requests that predate the meter, and 0 means uncounted rather than free.\n"
            "  Run `make crawl` (or `make crawl dispatch=1`) with a worker draining."
        )

    requests = sum(r.requests for r in counted)
    waited = sum(r.waited_seconds for r in counted)
    network = sum(r.network_seconds for r in counted)
    total = waited + network

    lines = [
        f"{requests} requests across {len(counted)} hosts",
        "",
        f"  waiting on the rate limiter  {waited:9.1f}s  {waited / total * 100:5.1f}%",
        f"  in the request itself        {network:9.1f}s  {network / total * 100:5.1f}%",
        f"  {'':<29}{total:9.1f}s",
        "",
    ]

    # The verdict, said outright. The two numbers are only useful together and
    # the reader should not have to know which way round is good.
    if network and waited / total > 0.9:
        lines.append("  Dominated by the floor, which is §2.6 working. There is no tuning here:")
        lines.append("  a shared ATS host is 2s and a company's own site is 60s, and the way to")
        lines.append("  finish sooner is more hosts in parallel, never less waiting per host.")
    else:
        pct = network / total * 100
        lines.append(f"  {pct:.0f}% of the time is network — the half worth looking at:")
        lines.append("  connection reuse, body size, and how many requests a company costs.")

    lines += [
        "",
        f"  {'host':<38} {'reqs':>6}  {'waited':>10} {'network':>10}"
        f" {'per req':>9} {'wait':>6}  floor",
    ]
    for r in counted[:limit]:
        lines.append(
            _row(
                r.host, r.requests, r.waited_seconds, r.network_seconds, r.host in SHARED_API_HOSTS
            )
        )
    if len(counted) > limit:
        lines.append(f"  ... and {len(counted) - limit} more hosts")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-n", "--limit", type=int, default=20, help="hosts to list")
    args = parser.parse_args(argv)
    print(asyncio.run(run(args.limit)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
