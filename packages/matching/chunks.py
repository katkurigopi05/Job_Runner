"""Split postings into fixed chunks and embed each one.

The recipe is the owner's DP-800 work, `AI_GENERATE_CHUNKS(CHUNK_TYPE = FIXED,
CHUNK_SIZE = 500, OVERLAP = 25)`, applied to job postings. Measured on the
5,922 postings of 2026-10-01 before it was built: a semantic search for "RAG"
went from 0 of the top 10 to 4, because one vector per posting covers only the
first 512 tokens and requirements come last. Fixed-size chunks rather than
paragraph-aware ones because the numbers above were measured with them, and
because postings arrive with their structure flattened by the extractors.

**Characters, not tokens.** 500 characters is about 110 tokens, well inside
bge-small's 512, so no chunk is truncated. The 25-character overlap means a
phrase cut by a boundary still appears whole in one of the two chunks, as long
as it is shorter than 25 characters, which a skill name is.

**Only a semantic model embeds chunks.** Chunk vectors from the lexical
embedder would only repeat what the keyword scan already reads. With
`EMBEDDING_BACKEND=lexical` this pass does nothing, and search falls back to
the one-vector-per-posting behaviour it had before.

**A chunk is offsets, not text.** See `core/models_chunks.py`. The offsets
point into `normalize(description_raw)`, and `content_hash` says which version
of the text: a posting whose hash moved is re-chunked, never read at old
offsets.
"""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Posting
from packages.core.models_chunks import PostingChunk
from packages.matching.embed import Embedder, SentenceTransformerEmbedder, get_embedder

log = structlog.get_logger(__name__)

#: `AI_GENERATE_CHUNKS(CHUNK_TYPE = FIXED, CHUNK_SIZE = 500, OVERLAP = 25)`.
CHUNK_SIZE = 500
OVERLAP = 25

#: Postings chunked per call. About 13.6 chunks each, so 500 postings is
#: roughly 7,000 vectors per call.
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


def chunk_embedder() -> Embedder | None:
    """bge-small when it is the configured backend and loads; otherwise None."""
    from packages.core.config import get_settings

    if get_settings().embedding_backend.lower() != "sentence-transformers":
        return None
    embedder = get_embedder()
    return embedder if isinstance(embedder, SentenceTransformerEmbedder) else None


async def chunk_postings(
    session: AsyncSession, *, embedder: Embedder | None = None, limit: int = DEFAULT_LIMIT
) -> int:
    """Chunk and embed open postings whose chunks are missing or stale.

    Returns how many postings were chunked. Does not commit. Newest postings
    first, so a backlog reaches the jobs the owner is most likely to ask about
    before the old ones.
    """
    active = embedder or chunk_embedder()
    if active is None:
        return 0
    stamp = active.name

    current = exists().where(
        PostingChunk.posting_id == Posting.id,
        PostingChunk.embedding_model == stamp,
        PostingChunk.content_hash.is_not_distinct_from(Posting.content_hash),
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
    # that is no longer there.
    await session.execute(
        delete(PostingChunk).where(PostingChunk.posting_id.in_([row.id for row in pending]))
    )

    texts: list[str] = []
    rows: list[PostingChunk] = []
    for posting_id, description, content_hash in pending:
        body = normalize(description or "")
        for ordinal, (start, length) in enumerate(spans(body)):
            texts.append(body[start : start + length])
            rows.append(
                PostingChunk(
                    posting_id=posting_id,
                    ordinal=ordinal,
                    start=start,
                    length=length,
                    content_hash=content_hash,
                    embedding_model=stamp,
                )
            )
    # Off the event loop: encoding thousands of chunks is seconds of CPU.
    vectors = await asyncio.to_thread(active.encode, texts)
    for row, vector in zip(rows, vectors, strict=True):
        row.embedding = vector
    session.add_all(rows)
    await session.flush()
    log.info("postings_chunked", postings=len(pending), chunks=len(rows), model=stamp)
    return len(pending)


__all__ = ["CHUNK_SIZE", "OVERLAP", "chunk_embedder", "chunk_postings", "normalize", "spans"]
