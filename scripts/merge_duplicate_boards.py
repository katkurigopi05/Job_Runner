"""Leave each board with one company row — `make merge-duplicate-boards`.

A dry run unless `--apply` (`make merge-duplicate-boards apply=1`), because it
deletes the second copy of every posting on a board two rows were polling.
Take a backup first (`make backup`). `packages/crawler/duplicate_boards.py`
says which row keeps a board, what is deleted and what never is.

A repair for a database older than `uq_companies_verified_board`. Once that
index exists the state this fixes cannot be written, and a run reports
nothing to do.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from packages.core import db as core_db
from packages.crawler.duplicate_boards import merge


async def _run(*, apply: bool) -> None:
    async with core_db.get_sessionmaker()() as session:
        report = await merge(session, now=datetime.now(UTC), apply=apply)
        if apply:
            await session.commit()
    for pair in report.pairs:
        print(f"  {pair}")
    print(("" if apply else "dry run: ") + report.summary())
    if not apply and report.boards:
        print("nothing was changed; run again with apply=1 after `make backup`")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the changes")
    asyncio.run(_run(apply=parser.parse_args().apply))


if __name__ == "__main__":
    main()
