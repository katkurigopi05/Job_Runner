"""Sort a CSV of companies into what we can crawl today — `make import-csv`.

    make import-csv src=companies.csv                 # report only
    make import-csv src=companies.csv write=1         # promote into the registry
    make import-csv src=companies.csv out=seeds/bespoke.csv

A sheet of company names and careers URLs is not 3,000 sites to scrape. Careers
URLs very often *are* a Greenhouse, Lever, Ashby or Workable board, and every
one of those needs no new code — it is a registry row the existing crawler
polls first-hand, on our own schedule, through the rate limiter and robots.

So this answers the question that decides how much work is left: **how many are
actually bespoke?** It runs offline, so the answer is the same whether or not
every site is up, and 3,000 rows take seconds.

What is left over is written to a CSV rather than counted and dropped. That
file is the work queue for a generic extractor — JSON-LD first, since career
sites publish `JobPosting` for Google Jobs and structured data beats a selector
that drifts.

Columns are detected, not assumed: any of `careers_url`, `career_page`,
`jobs_url`, `url`, `link`, `website` for the URL, and `company`, `name`,
`employer` for the label. If none matches, it says which columns the file
actually has rather than classifying everything as unusable.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from packages.crawler.company_csv import TriageReport, read_rows, triage, write_bespoke
from packages.crawler.extract import CompanySeed, default_seed_path, load_seed
from scripts.import_portals import append_to_registry

DEFAULT_BESPOKE = Path("seeds/bespoke_careers.csv")


def _additions(report: TriageReport, existing: list[CompanySeed]) -> list[CompanySeed]:
    """New registry rows, skipping anything already present.

    Matched on `(ats, slug)` rather than name: two sheets spell the same
    company differently, and the slug is what the crawler actually polls.
    Deciding that their "OpenAI" and ours are the same row on the strength of
    the name is a judgement this has no business making silently — the same
    line `import_portals` draws.
    """
    known = {(seed.ats, seed.slug.lower()) for seed in existing}
    additions: list[CompanySeed] = []
    for entry in report.promotable:
        assert entry.ats and entry.slug
        key = (entry.ats, entry.slug.lower())
        if key in known:
            continue
        known.add(key)
        additions.append(
            CompanySeed(name=entry.row.name or entry.slug, slug=entry.slug, ats=entry.ats)
        )
    return additions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv", type=Path, help="companies CSV")
    parser.add_argument("--seeds", type=Path, default=None, help="target registry file")
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_BESPOKE, help="where to write the bespoke remainder"
    )
    parser.add_argument(
        "--write", action="store_true", help="append the promotable rows to the registry"
    )
    args = parser.parse_args(argv)

    try:
        rows, header = read_rows(args.csv)
    except (OSError, ValueError) as exc:
        print(exc)
        return 1

    report = triage(rows)
    print(report.summary())

    seed_path = args.seeds or default_seed_path()
    existing = load_seed(str(seed_path))
    additions = _additions(report, existing)
    already = len(report.promotable) - len(additions)
    if already:
        print(f"  ({already} of those are already in {seed_path})")

    if report.bespoke:
        written = write_bespoke(report.bespoke, args.out, header)
        print(f"\nwrote {len(report.bespoke)} bespoke pages to {written}")
        print("  These need the generic extractor — JSON-LD and sitemaps — not a new adapter.")

    if report.unusable:
        print(f"\n{len(report.unusable)} rows carry no usable URL, for example:")
        for entry in report.unusable[:5]:
            print(f"  - {entry.row.name or '(unnamed)'}: {entry.reason}")

    if not additions:
        print("\nNothing new to add to the registry.")
        return 0

    print(f"\n{len(additions)} new companies to add:")
    for seed in additions[:10]:
        print(f"  + {seed.name} ({seed.ats}:{seed.slug})")
    if len(additions) > 10:
        print(f"  ... and {len(additions) - 10} more")

    if not args.write:
        print("\nreport only — pass write=1 to add them")
        return 0

    append_to_registry(additions, seed_path)
    print(f"\nwritten to {seed_path}. Run `make validate-seeds-write` to check the new slugs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
