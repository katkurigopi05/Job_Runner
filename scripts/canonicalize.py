"""Group stored listings that are confidently the same requisition.

    make canonicalize dry=1   # report what would be grouped, write nothing
    make canonicalize         # apply

The crawler groups new and edited listings as they arrive; this covers the
ones stored before grouping existed. Only companies with the same normalized
title on two or more *sources* are examined, because the rules never merge two
listings from one source. Nothing is deleted: a grouped listing keeps its row,
URL, matches and applications.
"""

from __future__ import annotations

import argparse
import asyncio


async def run(*, dry_run: bool = False) -> str:
    from sqlalchemy import select

    from packages.core.db import get_sessionmaker
    from packages.core.models import Posting
    from packages.matching.canonical import AssignReport, assign, source_key
    from packages.matching.titles import canonical as title_key

    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(
                    Posting.id, Posting.company_id, Posting.title, Posting.url, Posting.ats_type
                ).where(Posting.closed_at.is_(None), Posting.company_id.is_not(None))
            )
        ).all()
        sources: dict[tuple[object, str], set[str]] = {}
        members: dict[tuple[object, str], list[object]] = {}
        for row in rows:
            key = (row.company_id, title_key(row.title))
            sources.setdefault(key, set()).add(source_key(row))  # type: ignore[arg-type]
            members.setdefault(key, []).append(row.id)
        candidates = [
            posting_id
            for key, ids in members.items()
            if len(sources[key]) > 1
            for posting_id in ids
        ]
        report = await assign(session, candidates) if candidates else AssignReport()
        if dry_run:
            await session.rollback()
        else:
            await session.commit()
    prefix = "DRY RUN — nothing written. Would do: " if dry_run else ""
    return f"{prefix}{len(candidates)} listings examined; {report.summary()}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    print(asyncio.run(run(dry_run=args.dry_run)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
