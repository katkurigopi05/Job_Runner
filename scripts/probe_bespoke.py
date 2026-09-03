"""Probe the bespoke careers pages and promote the ones that publish jobs.

    make probe-bespoke                      # report only, whole file
    make probe-bespoke n=50                 # sample the first 50
    make probe-bespoke write=1              # add the pages that answered

`make import-csv` sorts a company sheet into boards we can already crawl and a
remainder in `seeds/bespoke_careers.csv`. This is what reads that file: it
fetches each page once through `PoliteFetcher` — so robots.txt and the per-host
floor apply — and asks whether the page publishes schema.org `JobPosting` data.

Only the pages that answered are promoted. `packages/crawler/bespoke.py` says
why a timeout or a 403 is not the same answer as "no structured data", and why
a page with none must not become a registry row.

It needs network egress from the owner's machine, like `make validate-seeds`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from packages.crawler.bespoke import BespokeState, ProbeReport, load_bespoke, probe, to_seeds
from packages.crawler.extract import default_seed_path, load_seed
from packages.crawler.fetch import build_fetcher
from scripts.import_portals import append_to_registry

DEFAULT_BESPOKE = Path("seeds/bespoke_careers.csv")


def _report(report: ProbeReport) -> None:
    """Print the verdict, naming examples rather than only counting.

    Blocked and unreachable pages are worth re-running; the no-data count is
    the measurement that says whether a sitemap strategy is worth building.
    """
    print(report.summary())

    for result in report.publishing[:10]:
        sample = f" — {result.titles[0]}" if result.titles else ""
        print(f"  + {result.name}: {result.postings} postings{sample}")
    if len(report.publishing) > 10:
        print(f"  ... and {len(report.publishing) - 10} more")

    for state in (BespokeState.NO_DATA, BespokeState.BLOCKED, BespokeState.UNREACHABLE):
        entries = report.of(state)
        if not entries:
            continue
        # Named rather than counted: BLOCKED and UNREACHABLE are worth
        # re-running, and NO_DATA is the measurement that says whether a
        # second strategy — sitemaps, then rendered HTML — is worth building.
        print(f"\n{len(entries)} {state.value}, for example:")
        for entry in entries[:5]:
            status = f" (HTTP {entry.status})" if entry.status else ""
            print(f"  - {entry.name}{status}")


async def _run(args: argparse.Namespace) -> int:
    """Probe the remainder file and, with --write, promote what published."""
    try:
        rows = load_bespoke(args.csv)
    except (OSError, ValueError) as exc:
        print(exc)
        return 1

    if not rows:
        print(f"{args.csv} is empty — run `make import-csv src=<sheet>` first.")
        return 0

    report = await probe(rows, build_fetcher(), limit=args.limit)

    _report(report)

    seed_path = args.seeds or default_seed_path()
    additions = to_seeds(report, load_seed(str(seed_path)))
    if not additions:
        print("\nNothing new to add to the registry.")
        return 0

    print(f"\n{len(additions)} new companies to add to {seed_path}.")
    if not args.write:
        print("report only — pass write=1 to add them")
        return 0

    append_to_registry(additions, seed_path)
    print(f"written to {seed_path}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for `make probe-bespoke`."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", type=Path, default=DEFAULT_BESPOKE)
    parser.add_argument("--seeds", type=Path, default=None, help="target registry file")
    parser.add_argument("-n", "--limit", type=int, default=None, help="probe only the first N")
    parser.add_argument(
        "--write", action="store_true", help="append the pages that publish to the registry"
    )
    args = parser.parse_args(argv)
    # A negative slice is not a smaller sample: `rows[:-1]` probes all but the
    # last page, which is the opposite of asking for fewer. Refused at the
    # boundary rather than silently reinterpreted.
    if args.limit is not None and args.limit < 0:
        parser.error("-n must be zero or more")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
