"""Company names in a CSV, job boards out.

    python -m scripts.find_boards bay-area.csv
    python -m scripts.find_boards bay-area.csv --append        # write to seeds
    python -m scripts.find_boards bay-area.csv --limit 20      # try a few first

The CSV needs a column of company names, and a column of URLs helps a great
deal. Headers `name`/`company`/`company_name` and `url`/`careers_url`/`website`
are recognised; with no recognised header the first column is taken as names
and row one is treated as data, since a headerless list is the likeliest thing
to have lying around.

**Bring the URLs if you have them.** A name is a guess — `acme.com` might be
`acmecorp`, `acme-inc`, or `getacme` on Greenhouse, and only the page knows
which. A careers URL is evidence: if it is already a board the slug is read
straight off it, and if it is the company's own page it is fetched once and
read for the ATS behind it.

**This is slow, and it is supposed to be.** Every request goes through the
polite fetcher, so robots.txt and the per-host floor apply — 2s between hits
on each ATS API host (§2.6 as amended). Several hundred names is tens of
minutes. `--limit` exists so the shape of the results can be checked before
committing to the whole file.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from pathlib import Path

from packages.crawler.company_csv import read_rows
from packages.crawler.find_boards import (
    VENDORS,
    Resolved,
    ResolveReport,
    resolve_all,
)


def read_companies(path: Path) -> list[tuple[str, str | None]]:
    """`(name, url)` pairs from a CSV, with or without a header.

    Delegates to `company_csv.read_rows` rather than carrying its own column
    lists, which is how the two readers drifted apart: this one scored columns
    by how many usable URLs they held, `company_csv` picked the first
    recognised name, and **neither knew `company_url`**. On the owner's
    3,869-row sheet the result was `company_csv` refusing the file outright and
    this function returning 3,869 rows with **0 URLs** — so every company fell
    to guessing a slug from its name and four speculative vendor probes, while
    `resolve_one` sat there preferring a supplied URL it was never given.

    The URL half is worth a great deal when present: it is evidence about which
    board a company uses, where the name is only a guess. `read_rows` resolves
    which column carries it, and drops a search link rather than passing one on
    as though it named a board.
    """
    try:
        rows, _ = read_rows(path)
    except ValueError:
        # `read_rows` raises with the header it found, which is the right
        # contract for an importer told to act on a named column. Here a file
        # with no recognised column is not a mistake: a bare list of company
        # names is the likeliest thing to have lying around, and assuming a
        # header would silently drop a real company. An empty file is no
        # companies rather than an error, for the same reason.
        return _headerless(path)
    return [(row.name, row.url or None) for row in rows]


def _headerless(path: Path) -> list[tuple[str, str | None]]:
    """First column as names, row one included — no header to skip."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [(row[0].strip(), None) for row in csv.reader(handle) if row and row[0].strip()]


def as_seed_entry(found: Resolved) -> str:
    return (
        f"  - name: {found.name}\n"
        f"    slug: {found.slug}\n"
        f"    ats: {found.ats}\n"
        f"    careers_url: {found.board_url}\n"
    )


def render(report: ResolveReport) -> str:
    lines: list[str] = []
    for found in sorted(report.resolved, key=lambda r: -r.open_jobs):
        lines.append(f"  {found.open_jobs:>4} jobs  {found.ats:<11} {found.name}  ({found.slug})")
    if report.blocked:
        lines.append("")
        lines.append("  blocked by robots.txt:")
        lines += [f"    {name} — {reason}" for name, reason in report.blocked]
    if report.unresolved:
        lines.append("")
        lines.append(f"  no board found ({len(report.unresolved)}):")
        # Listed in full rather than counted: these are real companies whose
        # careers pages we simply cannot read yet, and the list is the input
        # to deciding which extractor to write next.
        lines += [f"    {name}" for name, _ in report.unresolved]
    lines.append("")
    lines.append(f"  {report.summary()}")
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--limit", type=int, default=None, help="only try the first N names")
    parser.add_argument(
        "--append",
        action="store_true",
        help="append resolved companies to seeds/companies.yaml",
    )
    # Since vendors are probed concurrently, the sweep costs the *slowest*
    # host, so one slow vendor sets the pace for the whole file. On the
    # owner's 3,802-row CSV that is Workable: 3.9s per probe against ~1.5s
    # for the other three, because it is the only one returning 429 (four
    # penalties in the first 37 companies), and it resolved nothing at all in
    # a 60-company sample while taking 46% of the waiting.
    #
    # Dropping it is a real coverage loss, not a free win — genuine Workable
    # employers become invisible — so it is a flag rather than a new default.
    parser.add_argument(
        "--vendors",
        default=",".join(VENDORS),
        help=(
            "comma-separated ATS list to probe, in precedence order "
            f"(default: {','.join(VENDORS)}). Probed concurrently, so the run "
            "costs the slowest one — dropping `workable` roughly halves it, at "
            "the cost of missing Workable employers."
        ),
    )
    args = parser.parse_args()

    vendors = tuple(v.strip() for v in args.vendors.split(",") if v.strip())
    unknown = [v for v in vendors if v not in VENDORS]
    if unknown:
        print(f"unknown ATS: {', '.join(unknown)}. known: {', '.join(VENDORS)}", file=sys.stderr)
        return 1
    if not vendors:
        print("--vendors named nothing to probe", file=sys.stderr)
        return 1

    if not args.csv_path.is_file():
        print(f"no such file: {args.csv_path}", file=sys.stderr)
        return 1

    companies = read_companies(args.csv_path)
    if args.limit:
        companies = companies[: args.limit]
    if not companies:
        print("no company names found in that file", file=sys.stderr)
        return 1

    with_urls = sum(1 for _, url in companies if url)
    print(f"probing {len(companies)} companies across {', '.join(vendors)}")
    if with_urls:
        print(f"{with_urls} have a URL — resolved from evidence, not a guess")
    print("(2s per request per host — this takes a while)\n")

    def progress(name: str, outcome: object) -> None:
        mark = "hit " if isinstance(outcome, Resolved) else "  . "
        print(f"  {mark} {name}", flush=True)

    report = await resolve_all(companies, vendors=vendors, on_result=progress)
    print()
    print(render(report))

    if args.append and report.resolved:
        seeds = Path("seeds/companies.yaml")
        existing = seeds.read_text(encoding="utf-8")
        # Never rewrite what the owner curated; only add names not present.
        added = [f for f in report.resolved if f"name: {f.name}\n" not in existing]
        if added:
            with seeds.open("a", encoding="utf-8") as handle:
                handle.write("\n# Found by scripts/find_boards.py\n")
                for found in added:
                    handle.write(as_seed_entry(found))
        print(f"\n  appended {len(added)} new entries to {seeds}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
