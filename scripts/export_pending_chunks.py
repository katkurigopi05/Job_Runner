"""Write out the postings still missing Qwen vectors — `make export-chunk-vectors-pending`.

The file goes to a Colab TPU notebook (`scripts/colab/qwen_embed_batch.ipynb`),
whose vectors `make import-chunk-vectors` then attaches. Read-only: it writes a
file, never the database.

    make export-chunk-vectors-pending dry=1   # count only
    make export-chunk-vectors-pending         # write it to storage/qwen_batches.nosync/<date>/
    make export-chunk-vectors-pending all=1   # every posting, not only the owner's area

Only the owner's area is exported by default (`chunk_vectors.OWNER_AREA`): a
California office in any working mode, else remote in the United States.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from pathlib import Path

from packages.core import db as core_db
from packages.matching.chunk_vectors import OWNER_AREA, pending, write_export

#: `.nosync` keeps a batch out of iCloud, which syncs the owner's Desktop.
BATCHES = Path("storage/qwen_batches.nosync")
#: What the notebook writes and the import reads, beside the export.
EXPORT_NAME = "pending.json.gz"
VECTORS_NAME = "qwen4b_batch_f16.npy"
#: Chunks the TPU embedded per second on 2026-11-02 (one v5e chip).
TPU_CHUNKS_PER_S = 137


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="count only; write nothing")
    parser.add_argument(
        "--all", action="store_true", help="every posting, not only California or US-remote"
    )
    parser.add_argument(
        "--out", type=Path, default=None, help="folder for the batch (default: a dated one)"
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> None:
    args = _args(argv)
    async with core_db.get_sessionmaker()() as session:
        exported, report = await pending(session, scope=None if args.all else OWNER_AREA)
        await session.rollback()
    print(
        f"{report.covered:,} open postings have Qwen vectors; {report.postings:,} do not "
        f"({report.chunks:,} chunks); {report.not_chunked:,} wait for chunking first "
        "(the matching pass, or `make chunk-postings`)"
    )
    if report.out_of_scope:
        print(
            f"{report.out_of_scope:,} more are outside California and US-remote and are left "
            "out (all=1 exports them)"
        )
    if args.dry_run or not exported:
        if not exported:
            print("nothing to export")
        return
    folder = args.out or BATCHES / datetime.now(UTC).strftime("%Y-%m-%d-%H%M")
    path = folder / EXPORT_NAME
    write_export(path, exported)
    minutes = report.chunks / TPU_CHUNKS_PER_S / 60
    print(f"wrote {path} ({path.stat().st_size / 1e6:.1f} MB); about {minutes:.0f} min on the TPU")
    print("next:")
    print(f"  1. run scripts/colab/qwen_embed_batch.ipynb on a Colab TPU with {EXPORT_NAME}")
    print(f"  2. put the {VECTORS_NAME} it saves in {folder}/")
    print(f"  3. make import-chunk-vectors export={path} vectors={folder / VECTORS_NAME}")


if __name__ == "__main__":
    asyncio.run(main())
