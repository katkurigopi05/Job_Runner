"""Delete closed and old postings — `make prune-postings`.

A dry run unless `--apply` (`make prune-postings apply=1`), because a delete
cannot be taken back. Take a backup first (`make backup`); `packages/crawler/
retention.py` says what is deleted and what never is.

After a real run the tables that lost rows are compacted with VACUUM FULL.
A plain delete leaves the rows' space in the table, and a search that scans
it still reads every page, so the space would be reusable but the search no
faster.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from sqlalchemy import text

from packages.core import db as core_db
from packages.core.config import get_settings
from packages.crawler.retention import prune

#: The tables a prune shrinks: postings, and the two that cascade from it.
COMPACTED = ("postings", "matches", "posting_versions")


async def _size() -> str:
    async with core_db.get_sessionmaker()() as session:
        return str(
            await session.scalar(text("select pg_size_pretty(pg_total_relation_size('postings'))"))
        )


async def _compact() -> None:
    # VACUUM cannot run inside a transaction block.
    async with core_db.get_engine().connect() as connection:
        autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
        for table in COMPACTED:
            await autocommit.execute(text(f"VACUUM (FULL, ANALYZE) {table}"))


async def run(max_age_days: int, apply: bool) -> int:
    before = await _size()
    async with core_db.get_sessionmaker()() as session:
        report = await prune(session, max_age_days=max_age_days, now=datetime.now(UTC), apply=apply)
        if apply:
            await session.commit()
        else:
            await session.rollback()

    window = f"older than {max_age_days} days" if max_age_days > 0 else "of any age"
    print(f"closed or {window}: {report.stale}")
    print(
        f"  kept, you applied to, swiped on, tailored for, or graded them: {report.kept_for_owner}"
    )
    if not apply:
        print(f"  would delete: {report.stale - report.kept_for_owner}, leaving {report.remaining}")
        print("dry run; nothing deleted. `make prune-postings apply=1` deletes.")
        return 0

    await _compact()
    print(f"  deleted: {report.deleted}, leaving {report.remaining}")
    print(f"postings table: {before} -> {await _size()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=None,
        help="default: POSTING_MAX_AGE_DAYS; 0 deletes only closed postings",
    )
    parser.add_argument("--apply", action="store_true", help="delete; otherwise a dry run")
    args = parser.parse_args(argv)
    days = get_settings().posting_max_age_days if args.max_age_days is None else args.max_age_days
    return asyncio.run(run(days, args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
