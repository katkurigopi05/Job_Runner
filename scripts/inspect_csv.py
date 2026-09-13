"""Recount a company CSV, offline, so the numbers are reproducible.

    python -m scripts.inspect_csv bay_area_tech_companies_3_columns.csv

Reports what the file holds and — more usefully — what `read_rows` will make of
it, because a count of rows says nothing about how many the importer can act
on. Those two numbers were 3,869 and 0 before the column roles were fixed.

No network. The answer does not depend on which sites happen to be up.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

from packages.crawler.company_csv import classify_url, is_search_url, read_rows, triage


def _kind(url: str) -> str:
    """What a careers-column value actually is, by structure."""
    value = url.strip()
    if not value:
        return "blank"
    if not value.lower().startswith(("http://", "https://")):
        return "not http(s)"
    if is_search_url(value):
        return "search-result link (a hint)"
    if classify_url(value):
        return "direct board URL (needs validation)"
    return "other site"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path)
    args = parser.parse_args()
    if not args.csv_path.is_file():
        print(f"no such file: {args.csv_path}")
        return 1

    with args.csv_path.open(newline="", encoding="utf-8-sig") as handle:
        raw = list(csv.DictReader(handle))
    header = list(raw[0]) if raw else []

    print(f"file    : {args.csv_path}")
    print(f"headers : {header}")
    print(f"rows    : {len(raw)}")

    names = [(r.get("company_name") or "").strip() for r in raw]
    duplicate_names = {n: c for n, c in Counter(names).items() if c > 1 and n}
    exact = Counter(tuple((r.get(k) or "").strip() for k in header) for r in raw)
    print(f"  blank company_name   : {sum(1 for n in names if not n)}")
    print(
        f"  duplicate names      : {len(duplicate_names)} names over "
        f"{sum(duplicate_names.values())} rows"
    )
    print(f"  exact duplicate rows : {sum(c - 1 for c in exact.values() if c > 1)}")

    websites = [(r.get("company_url") or "").strip() for r in raw]
    print(f"  blank company_url    : {sum(1 for w in websites if not w)}")

    kinds = Counter(_kind(r.get("company_career_url") or "") for r in raw)
    print("\ncompany_career_url, by structure:")
    for kind, count in kinds.most_common():
        print(f"  {kind:<34} {count}")

    # A site:-scoped search names a domain. Where it equals the website column,
    # the careers value carries nothing the website does not already say.
    same = 0
    for row in raw:
        career = (row.get("company_career_url") or "").replace("%3A", ":")
        website = (row.get("company_url") or "").strip()
        if "site:" not in career or not website:
            continue
        scope = career.split("site:", 1)[1].split("+")[0].split("&")[0].strip().lower()
        host = (urlparse(website).hostname or "").lower().removeprefix("www.")
        if scope.removeprefix("www.") == host:
            same += 1
    print(f"\n  site:-scoped searches whose scope == company_url host: {same}")

    rows, _ = read_rows(args.csv_path)
    with_url = sum(1 for row in rows if row.url)
    print("\nwhat the importer will do with it:")
    print(f"  rows parsed                       : {len(rows)}")
    print(f"  carrying a lead (evidence path)   : {with_url}")
    print(f"  no lead (name-guess fallback only): {len(rows) - with_url}")
    print()
    print(triage(rows).summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
