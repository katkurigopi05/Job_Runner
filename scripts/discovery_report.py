"""`make discovery-report` — the companies discovery could not verify, and the fix each needs.

    python -m scripts.discovery_report                 # summary, and the CSV
    python -m scripts.discovery_report --out x.csv     # the CSV somewhere else

Read-only, in one snapshot. The CSV has one row per failed company: its
website, the careers page found on it, the category, the fix, the job system
that page is built on, and when discovery tries it next. Companies with a
careers page come first. `packages/crawler/discovery_report.py` says what the
categories mean.

A company that failed before categories were recorded shows as
`not_yet_rechecked` until its next discovery attempt.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
from pathlib import Path

from sqlalchemy import text

from packages.core.config import get_settings
from packages.core.db import get_engine, get_sessionmaker
from packages.crawler import discovery_report as report


def render(summary: report.Summary) -> str:
    lines = [
        f"{summary.total} companies not verified; "
        f"{summary.with_careers_page} have a careers page on record",
        "",
        f"{'companies':>10}  {'category':26} fix",
    ]
    for category, count in summary.by_category:
        lines.append(f"{count:>10}  {category:26} {report.fix_for(category)}")
    if summary.by_system:
        lines += ["", "careers pages on job systems discovery cannot read yet:"]
        lines += [f"{count:>10}  {system}" for system, count in summary.by_system]
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="where to write the CSV")
    args = parser.parse_args()
    out = args.out or Path(get_settings().storage_root) / "exports" / "discovery_failures.csv"

    try:
        async with get_sessionmaker()() as session:
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            failed = await report.failed_companies(session)
            await session.rollback()
    finally:
        await get_engine().dispose()

    print(render(report.summarise(failed)))
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=report.COLUMNS)
        writer.writeheader()
        writer.writerows(report.as_row(f) for f in failed)
    print(f"\nwritten {out} ({len(failed)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
