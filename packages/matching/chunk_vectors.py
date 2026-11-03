"""Import chunk vectors made elsewhere onto the chunks they were made from.

The owner embedded the whole index with Qwen3-Embedding-4B on a Colab TPU
(224,361 chunks in 27 minutes, 2026-11-02), from an export of each open
posting's chunk text. This attaches those vectors to `posting_chunks` rows as
`posting_chunk_vectors`. Nothing searches them yet.

**A vector goes only onto the text it was made from.** Between the export and
the import a posting can be edited, re-chunked, closed or deleted. A posting's
vectors are imported only when its chunk rows are current (their
`content_hash` is the posting's) and each one cuts exactly the exported text
out of today's description. Otherwise the posting is skipped and counted, and
a later export picks it up.

The vectors file is read row by row in the export's order: posting after
posting, each posting's chunks in reading order.
"""

from __future__ import annotations

import gzip
import json
import uuid
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Posting
from packages.core.models_chunks import LARGE_EMBEDDING_DIM, PostingChunk, PostingChunkVector
from packages.matching.chunks import normalize

log = structlog.get_logger(__name__)

#: The stamp Qwen3-Embedding-4B's vectors are stored under.
QWEN_4B = "Qwen/Qwen3-Embedding-4B"
#: Postings checked and written per transaction, so a stopped import keeps
#: what it wrote and a second run carries on.
BATCH_POSTINGS = 200


@dataclass(frozen=True)
class Exported:
    """One posting as the export wrote it: its id and its chunk texts in order."""

    id: str
    chunks: tuple[str, ...]


@dataclass(frozen=True)
class ImportReport:
    postings: int = 0
    #: Postings whose chunks matched the export, so their vectors apply.
    imported: int = 0
    #: Edited since the export, or chunked from another version of the text.
    changed: int = 0
    #: Open, with text, and no chunk rows yet.
    not_chunked: int = 0
    #: Closed, deleted, or never in this database.
    absent: int = 0
    chunks_imported: int = 0
    #: Already imported for this model.
    chunks_present: int = 0


def load_export(path: Path) -> tuple[Exported, ...]:
    """The export file's postings, in its order."""
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        data = json.load(handle)
    return tuple(
        Exported(id=str(posting["id"]), chunks=tuple(posting["chunks"]))
        for posting in data["postings"]
    )


def _check_shape(exported: Sequence[Exported], vectors: np.ndarray) -> None:
    if vectors.ndim != 2 or vectors.shape[1] != LARGE_EMBEDDING_DIM:
        raise ValueError(f"vectors must be N x {LARGE_EMBEDDING_DIM}; got {vectors.shape}")
    expected = sum(len(posting.chunks) for posting in exported)
    if vectors.shape[0] != expected:
        raise ValueError(
            f"the export has {expected:,} chunks and the vectors file {vectors.shape[0]:,} rows; "
            "they are not from the same run"
        )


def _as_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _matches(posting: Any, chunks: dict[int, Any], exported: Exported) -> bool:
    """Whether today's chunk rows cut exactly the exported text."""
    if set(chunks) != set(range(len(exported.chunks))):
        return False
    if any(row.content_hash != posting.content_hash for row in chunks.values()):
        return False
    body = normalize(posting.description_raw or "")
    return all(
        body[row.start : row.start + row.length] == exported.chunks[ordinal]
        for ordinal, row in chunks.items()
    )


async def _import_group(
    session: AsyncSession,
    group: Sequence[Exported],
    offsets: Sequence[int],
    vectors: np.ndarray,
    *,
    model: str,
    dry_run: bool,
) -> Counter[str]:
    ids = {posting.id: _as_uuid(posting.id) for posting in group}
    wanted = [value for value in ids.values() if value is not None]
    postings = {
        row.id: row
        for row in (
            await session.execute(
                select(
                    Posting.id, Posting.description_raw, Posting.content_hash, Posting.closed_at
                ).where(Posting.id.in_(wanted))
            )
        ).all()
    }
    chunk_rows = (
        await session.execute(
            select(
                PostingChunk.id,
                PostingChunk.posting_id,
                PostingChunk.ordinal,
                PostingChunk.start,
                PostingChunk.length,
                PostingChunk.content_hash,
            ).where(PostingChunk.posting_id.in_(wanted))
        )
    ).all()
    chunks_of: dict[uuid.UUID, dict[int, Any]] = defaultdict(dict)
    for row in chunk_rows:
        chunks_of[row.posting_id][row.ordinal] = row
    present = set(
        (
            await session.scalars(
                select(PostingChunkVector.chunk_id).where(
                    PostingChunkVector.embedding_model == model,
                    PostingChunkVector.chunk_id.in_([row.id for row in chunk_rows]),
                )
            )
        ).all()
    )

    counts: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    for exported, offset in zip(group, offsets, strict=True):
        posting = postings.get(ids[exported.id]) if ids[exported.id] else None
        if posting is None or posting.closed_at is not None:
            counts["absent"] += 1
            continue
        chunks = chunks_of.get(posting.id)
        if not chunks:
            counts["not_chunked"] += 1
            continue
        if not _matches(posting, chunks, exported):
            counts["changed"] += 1
            continue
        block = np.asarray(vectors[offset : offset + len(exported.chunks)])
        if not np.isfinite(block).all():
            raise ValueError(f"posting {exported.id} has a vector that is not finite")
        counts["imported"] += 1
        for ordinal in range(len(exported.chunks)):
            chunk_id = chunks[ordinal].id
            if chunk_id in present:
                counts["chunks_present"] += 1
            else:
                rows.append(
                    {"chunk_id": chunk_id, "embedding_model": model, "embedding": block[ordinal]}
                )
    counts["chunks_imported"] += len(rows)
    if rows and not dry_run:
        await session.execute(insert(PostingChunkVector).on_conflict_do_nothing(), rows)
    return counts


async def import_vectors(
    session: AsyncSession,
    exported: Sequence[Exported],
    vectors: np.ndarray,
    *,
    model: str = QWEN_4B,
    dry_run: bool = False,
    batch_postings: int = BATCH_POSTINGS,
) -> ImportReport:
    """Attach `vectors` to the chunks of each exported posting that still matches.

    `vectors` holds one row per exported chunk, in export order; a memory-mapped
    file is read a batch at a time. A dry run checks and counts, and writes
    nothing.
    """
    _check_shape(exported, vectors)
    offsets = np.cumsum([0, *(len(posting.chunks) for posting in exported)])[:-1]
    totals: Counter[str] = Counter()
    for first in range(0, len(exported), batch_postings):
        group = exported[first : first + batch_postings]
        totals += await _import_group(
            session,
            group,
            [int(offset) for offset in offsets[first : first + len(group)]],
            vectors,
            model=model,
            dry_run=dry_run,
        )
        if not dry_run:
            await session.commit()
        log.info(
            "chunk_vectors.batch",
            done=min(first + batch_postings, len(exported)),
            of=len(exported),
            chunks_imported=totals["chunks_imported"],
        )
    return ImportReport(postings=len(exported), **totals)


__all__ = [
    "BATCH_POSTINGS",
    "QWEN_4B",
    "Exported",
    "ImportReport",
    "import_vectors",
    "load_export",
]
