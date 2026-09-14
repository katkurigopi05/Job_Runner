"""Project the curated YAML registry into the database — `make registry-sync`.

    make registry-sync
    make registry-sync seeds=seeds/companies.yaml
    make registry-sync dry=1      # report only, writes nothing

The dispatching sweep reads the `companies` table; the registry lives in
`seeds/companies.yaml`. Nothing carried one into the other except the *inline*
crawl path, so a fresh database could not bootstrap: 105 seed entries, 0 company
rows, and a sweep that enqueued nothing while reporting "nothing due".

Explicit and idempotent on purpose. Run it at worker start or by hand; running
it twice changes nothing. It cannot reactivate a retired board — `load_seed`
reads `companies:` only, and `retired:` is used only to mark rows — and it will
not overwrite verification evidence newer than the seed file's own `checked`
stamp.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path


async def run(seed_path: str | None, *, dry_run: bool = False) -> str:
    from packages.core.db import get_sessionmaker
    from packages.crawler.extract import load_retired, load_seed
    from packages.crawler.registry import sync_registry

    seeds = load_seed(seed_path)
    retired = load_retired(seed_path)
    async with get_sessionmaker()() as session:
        outcome = await sync_registry(session, seeds, retired=retired)
        if dry_run:
            # Computed against the real rows, then thrown away: the preview is
            # the same code path as the write, so it cannot disagree with it.
            await session.rollback()
        else:
            await session.commit()
    prefix = "DRY RUN — nothing written. Would do: " if dry_run else ""
    return f"{prefix}{len(seeds)} registry entries and {len(retired)} retired: {outcome.summary()}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seeds", type=Path, default=None, help="registry file")
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would change and write nothing"
    )
    args = parser.parse_args(argv)
    print(asyncio.run(run(str(args.seeds) if args.seeds else None, dry_run=args.dry_run)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
