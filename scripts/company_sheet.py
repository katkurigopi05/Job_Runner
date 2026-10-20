"""`make company-sheet` — rebuild the owner's company workbook beside their CSV.

    python -m scripts.company_sheet bay_area_tech_companies.csv
    python -m scripts.company_sheet companies.csv --out elsewhere.xlsx

Writes `<csv name>.xlsx` next to the CSV: "All companies" (every row, and
whether its jobs are on Greenhouse, Ashby, Lever or Workable) and "Not on the
4 job boards" (the rest, with the careers pages and findings discovery and the
deep dive recorded). Read-only against the database; the CSV is not changed.
`packages/crawler/company_sheet.py` says what each column holds.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
from pathlib import Path

from sqlalchemy import text

from packages.core.db import get_engine, get_sessionmaker
from packages.crawler.company_sheet import all_row, load_companies, missing_row, write_workbook


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path, help="the company CSV that was imported")
    parser.add_argument("--out", type=Path, help="where to write the workbook")
    args = parser.parse_args()
    out = args.out or args.csv.with_suffix(".xlsx")

    with args.csv.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    try:
        async with get_sessionmaker()() as session:
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            companies = await load_companies(session, args.csv.name)
            await session.rollback()
    finally:
        await get_engine().dispose()

    every = [all_row(row, companies.get(number)) for number, row in enumerate(rows, 1)]
    missing = [
        line
        for number, row in enumerate(rows, 1)
        if (line := missing_row(row, companies.get(number))) is not None
    ]
    write_workbook(out, all_rows=every, missing_rows=missing, source_name=args.csv.name)
    on_board = sum(1 for line in every if line[3] == "Yes")
    print(f"{on_board} on a job board, {len(missing)} not; written {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
