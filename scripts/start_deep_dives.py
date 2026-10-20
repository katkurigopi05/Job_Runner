"""`make deep-dive` — start the deep dive into the noted companies.

    python -m scripts.start_deep_dives            # every noted company
    python -m scripts.start_deep_dives --limit 50 # the 50 noted longest ago
    python -m scripts.start_deep_dives --dry-run  # say how many, start none

The owner asked for the deep dive at the end, with their permission
(2026-10-19), so nothing starts it unless `DEEP_DIVE_AUTO` is set. The tasks
run after every other kind of work, discovery included.
"""

from __future__ import annotations

import argparse
import asyncio

from packages.core.db import get_engine, get_sessionmaker
from packages.crawler import issues


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        async with get_sessionmaker()() as session:
            counts = await issues.summary(session)
            print(
                f"{counts.waiting_for_permission} noted companies waiting for a deep dive; "
                f"{counts.needs_owner} on the error list; {counts.fixed} fixed"
            )
            if args.dry_run:
                return 0
            started = await issues.start_deep_dives(session, limit=args.limit)
            await session.commit()
            print(f"started {len(started)} deep dives; they run after discovery")
    finally:
        await get_engine().dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
