"""Attach Qwen chunk vectors made on a Colab TPU — `make import-chunk-vectors`.

The export and the vectors must be from the same run: the vectors file has one
row per exported chunk, in the export's order. Each posting is checked against
today's text before its vectors are written, and one that has moved on is
skipped and counted (`packages/matching/chunk_vectors.py`). Written in batches,
so stopping keeps what was written and running again carries on.

    make import-chunk-vectors dry=1   # check and count, write nothing
    make import-chunk-vectors         # write
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

import numpy as np

from packages.core import db as core_db
from packages.matching.chunk_vectors import QWEN_4B, import_vectors, load_export

#: The run of 2026-11-02 (Colab TPU v5e-1, 224,361 chunks).
RUN = Path("storage/embed_rank_bench/whole_index_2026-11-02")
DEFAULT_EXPORT = RUN / "whole_index.json.gz"
DEFAULT_VECTORS = RUN / "vectors.nosync" / "qwen4b_full_f16.npy"


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--export", type=Path, default=DEFAULT_EXPORT, help="the export file")
    parser.add_argument("--vectors", type=Path, default=DEFAULT_VECTORS, help="the .npy vectors")
    parser.add_argument("--model", default=QWEN_4B, help="stamp to store them under")
    parser.add_argument("--dry-run", action="store_true", help="check and count; write nothing")
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> None:
    args = _args(argv)
    for path in (args.export, args.vectors):
        if not path.exists():
            raise SystemExit(f"not found: {path}")
    exported = load_export(args.export)
    vectors = np.load(args.vectors, mmap_mode="r")
    print(
        f"{len(exported):,} exported postings, {vectors.shape[0]:,} vectors of "
        f"{vectors.shape[1]:,}; {'dry run, nothing written' if args.dry_run else 'writing'}"
    )
    started = time.perf_counter()
    async with core_db.get_sessionmaker()() as session:
        report = await import_vectors(
            session, exported, vectors, model=args.model, dry_run=args.dry_run
        )
    verb = "would import" if args.dry_run else "imported"
    print(
        f"{verb} {report.chunks_imported:,} chunk vectors for {report.imported:,} postings "
        f"({report.chunks_present:,} already there) in {time.perf_counter() - started:.0f} s"
    )
    print(
        f"skipped: {report.changed:,} changed since the export, {report.not_chunked:,} not "
        f"chunked yet, {report.absent:,} closed or gone"
    )


if __name__ == "__main__":
    asyncio.run(main())
