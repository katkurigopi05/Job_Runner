"""Split postings into fixed chunks, the windows Qwen vectors are made for.

The recipe is the owner's DP-800 work, `AI_GENERATE_CHUNKS(CHUNK_TYPE = FIXED,
CHUNK_SIZE = 500, OVERLAP = 25)`, applied to job postings. Its `OVERLAP` is a
percent of the chunk (Microsoft Learn: "the percentage of the preceding text"),
so 125 characters; until 2026-11-06 it was read as 25 characters. Measured on the
5,922 postings of 2026-10-01 before it was built: a semantic search for "RAG"
went from 0 of the top 10 to 4, because one vector per posting covers only the
first 512 tokens and requirements come last. Fixed-size chunks rather than
paragraph-aware ones because the numbers above were measured with them, and
because postings arrive with their structure flattened by the extractors.

**Characters, not tokens.** 500 characters is about 110 tokens, so no chunk
is truncated by any embedder. The 125-character overlap means a
phrase cut by a boundary still appears whole in one of the two chunks, as long
as it is shorter than 125 characters: a skill name, and most requirement lines.

**25% was measured, not only read** (2026-11-05). Qwen3-Embedding-4B on 398 of
the 500 paraphrased questions over their 392 target postings, a posting ranked
by its best chunk: MRR 0.832 at 5% (25 characters), 0.860 at 25%, +0.028
[+0.003, +0.053]; 10, 15, 20 and 30% were within noise of 5%. It costs about a
quarter more chunks (5,580 to 6,931 on that pool). A chunk records the overlap
it was cut at (`PostingChunk.overlap`), so changing `OVERLAP_PERCENT` re-cuts
every posting on the next pass and its Qwen vectors go with the old chunks.

**This pass embeds nothing.** It cut and embedded with bge-small until
2026-11-22, when the owner replaced bge-small with Qwen3-Embedding-4B for chunk
search. Qwen is too slow for this Mac (3.3 chunks a second), so the vectors are
made in Colab or Kaggle batches: `make export-chunk-vectors-pending` writes out
the chunks cut here, and `make import-chunk-vectors` attaches what comes back
(`matching/chunk_vectors.py`). Cutting is cheap, so it runs whatever
`EMBEDDING_BACKEND` says.

**A chunk is offsets, not text.** See `core/models_chunks.py`. The offsets
point into `normalize(description_raw)`, and `content_hash` says which version
of the text: a posting whose hash moved is re-chunked, never read at old
offsets.
"""

from __future__ import annotations

import time

import structlog
from sqlalchemy import delete, exists, func, insert, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Posting
from packages.core.models_chunks import PostingChunk

log = structlog.get_logger(__name__)

#: `AI_GENERATE_CHUNKS(CHUNK_TYPE = FIXED, CHUNK_SIZE = 500, OVERLAP = 25)`,
#: whose OVERLAP is a percent of CHUNK_SIZE.
CHUNK_SIZE = 500
OVERLAP_PERCENT = 25
#: Characters a chunk shares with the next: 125.
OVERLAP = CHUNK_SIZE * OVERLAP_PERCENT // 100

#: Postings chunked per call. About 17 chunks each, so 500 postings is
#: roughly 8,500 rows per call.
DEFAULT_LIMIT = 500


def normalize(text: str) -> str:
    """The text chunk offsets point into: runs of whitespace collapsed to one space."""
    return " ".join(text.split())


def spans(body: str) -> list[tuple[int, int]]:
    """`(start, length)` of each chunk of an already-normalised `body`."""
    if not body:
        return []
    out: list[tuple[int, int]] = []
    start = 0
    while True:
        out.append((start, min(CHUNK_SIZE, len(body) - start)))
        if start + CHUNK_SIZE >= len(body):
            return out
        start += CHUNK_SIZE - OVERLAP


async def chunk_postings(session: AsyncSession, *, limit: int = DEFAULT_LIMIT) -> int:
    """Chunk open postings whose chunks are missing or stale.

    Returns how many postings were chunked. Does not commit. Newest postings
    first, so a backlog reaches the jobs the owner is most likely to ask about
    before the old ones.
    """
    # A posting with no text has nothing to be searched by meaning. Chunks it
    # still holds were cut from text it has since lost (a listing edit clears
    # it until the job page is read again, and a page that failed is left
    # without), so they go now rather than wait for text that may not come.
    await session.execute(
        delete(PostingChunk).where(
            PostingChunk.posting_id.in_(
                select(Posting.id).where(
                    or_(
                        Posting.description_raw.is_(None), func.btrim(Posting.description_raw) == ""
                    )
                )
            )
        )
    )
    current = exists().where(
        PostingChunk.posting_id == Posting.id,
        PostingChunk.content_hash.is_not_distinct_from(Posting.content_hash),
        PostingChunk.overlap == OVERLAP,
    )
    pending = (
        await session.execute(
            select(Posting.id, Posting.description_raw, Posting.content_hash)
            .where(Posting.closed_at.is_(None), Posting.description_raw.is_not(None), ~current)
            .order_by(Posting.first_seen_at.desc(), Posting.id)
            .limit(limit)
        )
    ).all()
    if not pending:
        return 0

    # Stale chunks go first: a changed posting's old offsets point at text
    # that is no longer there. Their Qwen vectors go with them (cascade).
    await session.execute(
        delete(PostingChunk).where(PostingChunk.posting_id.in_([row.id for row in pending]))
    )

    rows = [
        {
            "posting_id": posting_id,
            "ordinal": ordinal,
            "start": start,
            "length": length,
            "content_hash": content_hash,
            "overlap": OVERLAP,
        }
        for posting_id, description, content_hash in pending
        for ordinal, (start, length) in enumerate(spans(normalize(description or "")))
    ]
    # One statement for the batch rather than an object per row, which nothing
    # here reads back. Guarded, because executed with no rows it would insert
    # one of defaults.
    started = time.perf_counter()
    if rows:
        await session.execute(insert(PostingChunk), rows)
    log.info(
        "postings_chunked",
        postings=len(pending),
        chunks=len(rows),
        write_seconds=round(time.perf_counter() - started, 3),
    )
    return len(pending)


__all__ = ["CHUNK_SIZE", "OVERLAP", "chunk_postings", "normalize", "spans"]
