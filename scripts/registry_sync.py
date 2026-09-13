"""Project the curated YAML registry into the database — `make registry-sync`.

    make registry-sync
    make registry-sync seeds=seeds/companies.yaml

The dispatching sweep reads the `companies` table; the registry lives in
`seeds/companies.yaml`. Nothing carried one into the other except the *inline*
crawl path, so a fresh database could not bootstrap: 105 seed entries, 0 company
rows, and a sweep that enqueued nothing while reporting "nothing due".

Explicit and idempotent on purpose. Run it at worker start or by hand; running
it twice changes nothing. It cannot reactivate a retired board — `load_seed`
reads `companies:` only — and it will not overwrite verification evidence newer
than the seed file's own `checked` stamp.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path


async def run(seed_path: str | None) -> str:
    from packages.core.db import get_sessionmaker
    from packages.crawler.extract import load_seed
    from packages.crawler.registry import sync_registry

    seeds = load_seed(seed_path)
    async with get_sessionmaker()() as session:
        outcome = await sync_registry(session, seeds)
        await session.commit()
    return f"{len(seeds)} registry entries: {outcome.summary()}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seeds", type=Path, default=None, help="registry file")
    args = parser.parse_args(argv)
    print(asyncio.run(run(str(args.seeds) if args.seeds else None)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
