"""One discovery sweep, run by hand — `make discover`.

The worker runs this on a schedule; this is for the first sweep, and for
seeing what promotion is about to add before trusting it to a queue.
"""

from __future__ import annotations

import asyncio

from packages.core import db as core_db
from packages.crawler.discover import ingest, promote
from packages.crawler.fetch import build_fetcher


async def main() -> None:
    async with core_db.get_sessionmaker()() as session:
        report = await ingest(session, build_fetcher())
        print(report.summary())

        for result in report.sources:
            if not result.ok:
                print(f"  {result.source}: {result.failure}")

        added = await promote(session)
        for seed in added:
            print(f"  + {seed.name} ({seed.ats}:{seed.slug})")

        await session.commit()
        print(f"{len(added)} companies added to the registry")


if __name__ == "__main__":
    asyncio.run(main())
