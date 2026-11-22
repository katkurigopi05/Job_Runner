"""Qwen3-Embedding-4B Redis namespace and resumable posting cache sync."""

from __future__ import annotations

import hashlib
import json
import struct
import uuid
from time import perf_counter
from typing import Any

from sqlalchemy import Select, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Posting
from packages.core.models_chunks import LARGE_EMBEDDING_DIM, PostingChunk, PostingChunkVector
from packages.matching.chunk_vectors import QWEN_4B
from packages.matching.chunks import OVERLAP
from packages.matching.vector_search import (
    DEFAULT_FILTERS,
    Filters,
    Hit,
    ensure_redis_index,
    model_tag,
    redis_client,
    redis_filter,
    redis_knn,
    vector_blob,
)

INDEX = "jobrunner:chunks:qwen3-embedding-4b:2560:v1"


def query_vector(values: list[float]) -> tuple[list[float], bytes]:
    dim = LARGE_EMBEDDING_DIM
    vector_blob(values, dim=dim)
    quantized = list(struct.unpack(f"<{dim}e", struct.pack(f"<{dim}e", *values)))
    return quantized, vector_blob(quantized, dim=dim)


def postings() -> Select[tuple[uuid.UUID]]:
    return (
        select(PostingChunkVector.chunk_id)
        .select_from(PostingChunkVector)
        .join(PostingChunk, PostingChunk.id == PostingChunkVector.chunk_id)
        .join(Posting, Posting.id == PostingChunk.posting_id)
        .where(
            PostingChunkVector.embedding_model == QWEN_4B,
            PostingChunk.content_hash.is_not_distinct_from(Posting.content_hash),
            PostingChunk.overlap == OVERLAP,
        )
    )


async def search(
    session: AsyncSession,
    values: list[float],
    *,
    k: int = 10,
    filters: Filters = DEFAULT_FILTERS,
    redis: bool = False,
    client: Any = None,
    index: str = INDEX,
) -> list[Hit]:
    vector, blob = query_vector(values)
    distance = PostingChunkVector.embedding.cosine_distance(vector)
    query = (
        postings()
        .add_columns(PostingChunk.posting_id, PostingChunk.start, PostingChunk.length, distance)
        .where(distance < 2.01)
    )  # also excludes pgvector's NaN cosine for zero vectors
    if filters.companies:
        query = query.where(Posting.company_id.in_(filters.companies))
    if filters.open_only:
        query = query.where(Posting.closed_at.is_(None))
    if filters.remote is not None:
        query = query.where(Posting.is_remote.is_(filters.remote))
    if redis:
        owned = client is None
        client = redis_client() if owned else client
        try:
            if not await client.get(f"{index}:synced"):
                raise RuntimeError("Qwen Redis posting index is not synced.")
            ids = await redis_knn(
                client,
                index,
                redis_filter(QWEN_4B, filters),
                blob,
                max(2 * k, 10),
                field="chunk_id",
            )
        finally:
            if owned:
                await client.aclose()
        query = query.where(PostingChunkVector.chunk_id.in_(ids))
    rows = await session.execute(
        query.order_by(distance + 0.0, PostingChunkVector.chunk_id).limit(k)
    )
    return [Hit(cid, pid, start, length, float(dist)) for cid, pid, start, length, dist in rows]


async def write_documents(
    client: Any, index: str, documents: list[tuple[uuid.UUID, dict[str, str], Any]]
) -> int:
    encoded: list[tuple[str, dict[str, Any]]] = []
    for doc_id, fields, values in documents:
        values = [float(value) for value in values]
        if not any(values):
            continue
        blob = vector_blob(values, dim=LARGE_EMBEDDING_DIM)
        digest = hashlib.sha256(json.dumps(fields, sort_keys=True).encode() + blob).hexdigest()
        encoded.append((f"{index}:doc:{doc_id}", {**fields, "embedding": blob, "digest": digest}))
    async with client.pipeline(transaction=False) as pipe:
        for name, _ in encoded:
            pipe.hget(name, "digest")
        existing = await pipe.execute()
    writes = [
        (name, doc)
        for (name, doc), old in zip(encoded, existing, strict=True)
        if old != doc["digest"].encode()
    ]
    async with client.pipeline(transaction=False) as pipe:
        for name, doc in writes:
            pipe.hset(name, mapping=doc)
        await pipe.execute()
    return len(writes)


async def sync(
    session: AsyncSession,
    client: Any,
    *,
    batch_size: int = 500,
    max_batches: int | None = None,
    index: str = INDEX,
    rebuild: bool = False,
) -> dict[str, Any]:
    """Use a fresh session. The cursor advances only after a batch was written."""
    if batch_size < 1 or (max_batches is not None and max_batches < 1):
        raise ValueError("Batch sizes and limits must be positive.")
    start = perf_counter()
    if not await session.scalar(text("SELECT pg_try_advisory_xact_lock(731942118)")):
        raise RuntimeError("Another Qwen posting sync is running.")
    cursor = f"{index}:cursor"
    marker = f"{index}:synced"
    if rebuild:
        await client.delete(cursor, marker)
        try:
            await client.execute_command("FT.DROPINDEX", index, "DD")
        except Exception as exc:
            if "unknown index" not in str(exc).lower():
                raise
    await ensure_redis_index(client, index=index, dim=LARGE_EMBEDDING_DIM)
    saved = await client.get(cursor)
    last = uuid.UUID(saved.decode()) if saved else None
    stats = {"scanned": 0, "written": 0, "deleted": 0, "complete": False}
    query = postings().add_columns(
        PostingChunkVector.embedding, Posting.company_id, Posting.is_remote, Posting.closed_at
    )
    batches = 0
    while max_batches is None or batches < max_batches:
        page = query.where(PostingChunkVector.chunk_id > last) if last else query
        rows = (
            await session.execute(page.order_by(PostingChunkVector.chunk_id).limit(batch_size))
        ).all()
        if not rows:
            stats["deleted"] = await prune(session, client, index, postings(), batch_size)
            async with client.pipeline(transaction=True) as pipe:
                pipe.set(marker, "1")
                pipe.delete(cursor)
                await pipe.execute()
            stats["complete"] = True
            break
        docs = [
            (
                cid,
                {
                    "chunk_id": str(cid),
                    "model": model_tag(QWEN_4B),
                    "company": company.hex if company else "none",
                    "remote": "unknown" if remote is None else "yes" if remote else "no",
                    "open": "yes" if closed is None else "no",
                },
                vector,
            )
            for cid, vector, company, remote, closed in rows
        ]
        stats["written"] += await write_documents(client, index, docs)
        stats["scanned"] += len(rows)
        last = rows[-1][0]
        await client.set(cursor, str(last))
        batches += 1
    return {**stats, "seconds": round(perf_counter() - start, 4)}


async def prune(
    session: AsyncSession, client: Any, index: str, valid: Select[tuple[uuid.UUID]], size: int
) -> int:
    """Delete only derived documents whose durable source no longer exists."""
    deleted = 0
    cursor = 0
    prefix = f"{index}:doc:"
    while True:
        cursor, names = await client.scan(cursor, match=f"{prefix}*", count=size)
        identifiers = {uuid.UUID(name.decode()[len(prefix) :]): name for name in names}
        if identifiers:
            source_column = valid.selected_columns[0]
            current = set(await session.scalars(valid.where(source_column.in_(identifiers))))
            stale = [name for identifier, name in identifiers.items() if identifier not in current]
            if stale:
                deleted += await client.delete(*stale)
        if not cursor:
            return deleted
