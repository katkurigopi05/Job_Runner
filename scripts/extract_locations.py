"""Backfill structured locations locally; resumable, with no source-text edits.

Run: python -m scripts.extract_locations [--limit 500]
Workday offices not present in stored text resolve during subsequent detail reads.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter

from sqlalchemy import or_, select, update

from packages.core.db import get_sessionmaker
from packages.core.models import Posting
from packages.matching.location_fields import LOCATION_VERSION, normalize


async def run(*, limit: int | None = None, batch_size: int = 500) -> dict[str, int]:
    if batch_size < 1 or (limit is not None and limit < 1):
        raise ValueError("limit and batch_size must be positive")
    counts: Counter[str] = Counter()
    async with get_sessionmaker()() as session:
        while limit is None or counts["processed"] < limit:
            size = batch_size if limit is None else min(batch_size, limit - counts["processed"])
            rows = (
                await session.execute(
                    select(
                        Posting.id,
                        Posting.title,
                        Posting.location,
                        Posting.description_raw,
                        Posting.locations_json,
                    )
                    .where(
                        or_(
                            Posting.location_version.is_(None),
                            Posting.location_version < LOCATION_VERSION,
                        )
                    )
                    .order_by(Posting.id)
                    .limit(size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            if not rows:
                break
            updates = []
            for row in rows:
                fields = normalize(
                    row.location,
                    title=row.title,
                    description=row.description_raw,
                    source_locations=row.locations_json,
                )
                updates.append({"id": row.id, **fields})
                counts["processed"] += 1
                for name in ("country_code", "state_code", "is_remote"):
                    counts[name] += fields[name] is not None
            await session.execute(update(Posting), updates)
            await session.commit()
            print(f"{counts['processed']} postings normalized", flush=True)
    return dict(counts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    print(asyncio.run(run(limit=args.limit)))


if __name__ == "__main__":
    main()
