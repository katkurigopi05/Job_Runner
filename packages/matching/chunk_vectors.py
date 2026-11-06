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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import structlog
from sqlalchemy import and_, exists, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Posting
from packages.core.models_chunks import LARGE_EMBEDDING_DIM, PostingChunk, PostingChunkVector
from packages.matching.chunks import normalize
from packages.matching.locality import california_or_us_remote

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


@dataclass(frozen=True)
class PendingReport:
    #: Exported: open, with text, chunked from today's text, a chunk without a vector.
    postings: int = 0
    chunks: int = 0
    #: Open postings whose every current chunk has a vector already.
    covered: int = 0
    #: Open postings with text whose chunks are missing or cut from an older
    #: text. The matching pass (or `make chunk-postings`) chunks them first.
    not_chunked: int = 0
    #: Postings the export's scope left out, chunked or not.
    out_of_scope: int = 0


#: Which postings an export may hold, read from title, location and description.
Scope = Callable[[str | None, str | None, str | None], bool]


def _owner_area(title: str | None, location: str | None, description: str | None) -> bool:
    return california_or_us_remote(title=title, location=location, description=description)


#: What gets Qwen vectors (owner, 2026-11-05): a California office in any
#: working mode, else remote in the United States. Never "2 Locations".
OWNER_AREA: Scope = _owner_area


def _keeps(scope: Scope | None, title: str | None, location: str | None, body: str | None) -> bool:
    return scope is None or scope(title, location, body)


async def _not_chunked(session: AsyncSession, scope: Scope | None) -> tuple[int, int]:
    """Postings waiting to be chunked: how many the scope keeps, and how many it leaves out."""
    if scope is None:
        waiting = await session.scalar(select(func.count()).where(_with_text(), ~_chunked()))
        return int(waiting or 0), 0
    rows = (
        await session.execute(
            select(Posting.title, Posting.location, Posting.description_raw).where(
                _with_text(), ~_chunked()
            )
        )
    ).all()
    kept = sum(1 for row in rows if scope(row.title, row.location, row.description_raw))
    return kept, len(rows) - kept


def _with_text() -> Any:
    return and_(
        Posting.closed_at.is_(None),
        Posting.description_raw.is_not(None),
        func.btrim(Posting.description_raw) != "",
    )


def _chunked() -> Any:
    """Chunk rows exist, and every one was cut from the text the posting holds now."""
    current = exists().where(
        PostingChunk.posting_id == Posting.id,
        PostingChunk.content_hash.is_not_distinct_from(Posting.content_hash),
    )
    stale = exists().where(
        PostingChunk.posting_id == Posting.id,
        PostingChunk.content_hash.is_distinct_from(Posting.content_hash),
    )
    return and_(current, ~stale)


def _missing_vector(model: str) -> Any:
    """At least one current chunk has no vector from `model`."""
    vector = and_(
        PostingChunkVector.chunk_id == PostingChunk.id, PostingChunkVector.embedding_model == model
    )
    return exists(
        select(1)
        .select_from(PostingChunk)
        .outerjoin(PostingChunkVector, vector)
        .where(PostingChunk.posting_id == Posting.id, PostingChunkVector.chunk_id.is_(None))
        .correlate(Posting)
    )


async def pending(
    session: AsyncSession, *, model: str = QWEN_4B, scope: Scope | None = None
) -> tuple[tuple[Exported, ...], PendingReport]:
    """Every open posting whose current chunks are missing a vector, as the importer reads them.

    A posting is exported whole, so its rows in the vectors file line up with
    its chunks; chunks that already have a vector are skipped on import. A
    posting not chunked from today's text is counted and left for the
    matching pass, which chunks it with bge-small first. `scope` keeps only
    the postings it accepts (`OWNER_AREA`); the rest are counted, not exported.
    """
    found = (
        await session.execute(
            select(Posting.id, Posting.title, Posting.location, Posting.description_raw)
            .where(_with_text(), _chunked(), _missing_vector(model))
            .order_by(Posting.first_seen_at, Posting.id)
        )
    ).all()
    rows = [
        (row.id, row.description_raw)
        for row in found
        if _keeps(scope, row.title, row.location, row.description_raw)
    ]
    spans: dict[uuid.UUID, list[tuple[int, int, int]]] = defaultdict(list)
    ids = [posting_id for posting_id, _ in rows]
    for first in range(0, len(ids), 1000):
        for chunk in (
            await session.execute(
                select(
                    PostingChunk.posting_id,
                    PostingChunk.ordinal,
                    PostingChunk.start,
                    PostingChunk.length,
                ).where(PostingChunk.posting_id.in_(ids[first : first + 1000]))
            )
        ).all():
            spans[chunk.posting_id].append((chunk.ordinal, chunk.start, chunk.length))
    exported = []
    for posting_id, description in rows:
        body = normalize(description or "")
        ordered = sorted(spans[posting_id])
        exported.append(
            Exported(
                id=str(posting_id),
                chunks=tuple(body[start : start + length] for _, start, length in ordered),
            )
        )
    covered = await session.scalar(
        select(func.count()).where(_with_text(), _chunked(), ~_missing_vector(model))
    )
    not_chunked, unchunked_out = await _not_chunked(session, scope)
    report = PendingReport(
        postings=len(exported),
        chunks=sum(len(posting.chunks) for posting in exported),
        covered=int(covered or 0),
        not_chunked=not_chunked,
        out_of_scope=len(found) - len(rows) + unchunked_out,
    )
    return tuple(exported), report


def write_export(path: Path, exported: Sequence[Exported]) -> None:
    """The file the Colab notebook embeds and `import_vectors` checks against."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "made": datetime.now(UTC).isoformat(timespec="seconds"),
        "about": "Chunks of open postings without a Qwen vector, cut 500/25 as the app "
        "cuts them. Public posting text only.",
        "postings": [{"id": posting.id, "chunks": list(posting.chunks)} for posting in exported],
        "questions": [],
    }
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as handle:
        json.dump(data, handle, ensure_ascii=False)


__all__ = [
    "BATCH_POSTINGS",
    "QWEN_4B",
    "Exported",
    "ImportReport",
    "PendingReport",
    "import_vectors",
    "load_export",
    "pending",
    "write_export",
]
