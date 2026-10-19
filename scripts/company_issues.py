"""`make company-issues` — companies a worker noted, and the errors the deep dive left.

    python -m scripts.company_issues                # summary, and both CSVs
    python -m scripts.company_issues --dir x/       # the CSVs somewhere else

Two lists, as the owner asked for them (2026-10-19):

- `companies_noted.csv`: companies a worker could not read and moved past,
  waiting for the deep dive, which runs once nothing else is waiting.
- `company_errors.csv`: companies the deep dive looked into and could not
  fix, each with what it found and what to do, grouped by kind.

Read-only, in one snapshot.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
from collections.abc import Iterable
from pathlib import Path

from sqlalchemy import text

from packages.core.config import get_settings
from packages.core.db import get_engine, get_sessionmaker
from packages.crawler import issue_report as report


def render(lists: report.Lists) -> str:
    lines = [
        f"{len(lists.noted)} noted, waiting for a deep dive",
        f"{len(lists.errors)} on the error list",
        f"{lists.fixed_by_deep_dive} fixed by the deep dive, "
        f"{lists.fixed_by_later_run} by an ordinary run later",
    ]
    if lists.errors_by_kind:
        lines += ["", f"{'companies':>10}  error list, by what is wrong"]
        lines += [f"{count:>10}  {kind}" for kind, count in lists.errors_by_kind]
    return "\n".join(lines)


def _write(path: Path, columns: tuple[str, ...], rows: Iterable[dict[str, str]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
            written += 1
    return written


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, help="where to write the two CSVs")
    args = parser.parse_args()
    out = args.dir or Path(get_settings().storage_root) / "exports"

    try:
        async with get_sessionmaker()() as session:
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            lists = await report.read_lists(session)
            await session.rollback()
    finally:
        await get_engine().dispose()

    print(render(lists))
    noted = _write(
        out / "companies_noted.csv",
        report.NOTED_COLUMNS,
        (report.noted_row(x) for x in lists.noted),
    )
    errors = _write(
        out / "company_errors.csv",
        report.ERROR_COLUMNS,
        (report.error_row(x) for x in lists.errors),
    )
    print(f"\nwritten {out / 'companies_noted.csv'} ({noted} rows)")
    print(f"written {out / 'company_errors.csv'} ({errors} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
