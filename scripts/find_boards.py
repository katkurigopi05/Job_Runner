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

from packages.crawler.find_boards import Resolved, ResolveReport, resolve_all

NAME_COLUMNS = ("name", "company", "company_name", "companies", "employer")
URL_COLUMNS = ("url", "careers_url", "website", "link", "careers", "site", "homepage")


def read_companies(path: Path) -> list[tuple[str, str | None]]:
    """`(name, url)` pairs from a CSV, with or without a header.

    The URL half is optional and worth a great deal when present: it is
    evidence about which board a company uses, where the name is only a guess.
    A row with a URL and no name still counts — the name is used for display
    and for the fallback guess, nothing else.
    """
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.reader(handle))

    if not rows:
        return []

    header = [cell.strip().lower() for cell in rows[0]]
    name_index = next((header.index(c) for c in NAME_COLUMNS if c in header), None)
    url_index = next((header.index(c) for c in URL_COLUMNS if c in header), None)

    if name_index is None and url_index is None:
        # No recognised header: first column is names, and row one is data.
        # Assuming a header exists would silently drop a real company.
        return [(row[0].strip(), None) for row in rows if row and row[0].strip()]

    def cell(row: list[str], index: int | None) -> str | None:
        if index is None or len(row) <= index:
            return None
        return row[index].strip() or None

    out: list[tuple[str, str | None]] = []
    for row in rows[1:]:
        name = cell(row, name_index)
        url = cell(row, url_index)
        if not name and not url:
            continue
        out.append((name or (url or ""), url))
    return out


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
    args = parser.parse_args()

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
    print(f"probing {len(companies)} companies across greenhouse, lever, ashby, workable")
    if with_urls:
        print(f"{with_urls} have a URL — resolved from evidence, not a guess")
    print("(2s per request per host — this takes a while)\n")

    def progress(name: str, outcome: object) -> None:
        mark = "hit " if isinstance(outcome, Resolved) else "  . "
        print(f"  {mark} {name}", flush=True)

    report = await resolve_all(companies, on_result=progress)
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
