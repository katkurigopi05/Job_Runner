"""Copy Qwen vectors onto postings that repeat embedded text — `make copy-chunk-vectors`.

A posting whose description equals one with Qwen vectors, chunked at the same
offsets, takes those vectors instead of being embedded again
(`chunk_vectors.copy_vectors`). `make import-chunk-vectors` does this after each
batch; this runs it alone, when a crawl has added repeats of embedded text.

    make copy-chunk-vectors dry=1   # count only
    make copy-chunk-vectors         # copy, the owner's area only
    make copy-chunk-vectors all=1   # every posting
"""

from __future__ import annotations

import argparse
import asyncio
import time

from packages.core import db as core_db
from packages.matching.chunk_vectors import OWNER_AREA, QWEN_4B, copy_vectors


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="count only; write nothing")
    parser.add_argument(
        "--all", action="store_true", help="every posting, not only California or US-remote"
    )
    parser.add_argument("--model", default=QWEN_4B, help="the vectors' stamp")
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> None:
    args = _args(argv)
    started = time.perf_counter()
    async with core_db.get_sessionmaker()() as session:
        report = await copy_vectors(
            session,
            model=args.model,
            scope=None if args.all else OWNER_AREA,
            dry_run=args.dry_run,
        )
        if args.dry_run:
            await session.rollback()
        else:
            await session.commit()
    print(
        f"{'would copy' if args.dry_run else 'copied'} {report.chunks:,} chunk vectors onto "
        f"{report.postings:,} postings in {time.perf_counter() - started:.1f} s"
    )


if __name__ == "__main__":
    asyncio.run(main())
