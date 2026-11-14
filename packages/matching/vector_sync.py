"""Incremental Redis writes with full source reconciliation, including deletions.

No timestamp watermark: posting metadata has no updated_at and chunks can be
replaced/deleted. A repeatable-read snapshot and an advisory lock serialize
syncs; failed passes are safe to retry. Only hashes whose digest changed are
written. No Redis failure can roll back a PostgreSQL application transaction.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Posting
from packages.core.models_chunks import PostingChunk
from packages.matching.vector_search import INDEX, ensure_redis_index, model_tag, vector_blob


async def sync(
    session: AsyncSession, client: Any, *, batch_size: int = 500, index: str = INDEX
) -> dict[str, int]:
    """Use a fresh session; caller commits/releases the snapshot and sync lock."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
    locked = await session.scalar(text("SELECT pg_try_advisory_xact_lock(731942108)"))
    if not locked:
        raise RuntimeError("another vector sync is running")
    await ensure_redis_index(client, index=index)
    prefix = f"{index}:doc:"
    stats = {"scanned": 0, "written": 0, "deleted": 0}
    seen: set[bytes] = set()
    last: uuid.UUID | None = None
    while True:
        query = (
            select(PostingChunk, Posting.company_id, Posting.is_remote, Posting.closed_at)
            .join(Posting, Posting.id == PostingChunk.posting_id)
            .where(PostingChunk.content_hash.is_not_distinct_from(Posting.content_hash))
        )
        if last is not None:
            query = query.where(PostingChunk.id > last)
        rows = (await session.execute(query.order_by(PostingChunk.id).limit(batch_size))).all()
        if not rows:
            break
        documents = []
        for chunk, company, remote, closed in rows:
            last = chunk.id
            stats["scanned"] += 1
            values = [float(v) for v in chunk.embedding]
            # pgvector excludes zero vectors from cosine indexes too.
            if not any(values):
                continue
            key = f"{prefix}{chunk.id}".encode()
            doc = {
                "chunk_id": str(chunk.id),
                "model": model_tag(chunk.embedding_model),
                "company": company.hex if company else "none",
                "remote": "unknown" if remote is None else "yes" if remote else "no",
                "open": "yes" if closed is None else "no",
                "posting_id": str(chunk.posting_id),
                "start": str(chunk.start),
                "length": str(chunk.length),
                "content_hash": chunk.content_hash or "",
            }
            blob = vector_blob(values)
            digest = hashlib.sha256(json.dumps(doc, sort_keys=True).encode() + blob).hexdigest()
            documents.append((key, {**doc, "embedding": blob, "digest": digest}))
            seen.add(key)
        async with client.pipeline(transaction=False) as pipe:
            for key, _ in documents:
                pipe.hget(key, "digest")
            old = await pipe.execute()
        async with client.pipeline(transaction=False) as pipe:
            for (key, doc), prior in zip(documents, old, strict=True):
                if prior != doc["digest"].encode():
                    pipe.hset(key, mapping=doc)
                    stats["written"] += 1
            await pipe.execute()
    # SCAN instead of KEYS; delete in bounded batches. Only this namespace.
    stale = []
    async for key in client.scan_iter(match=f"{prefix}*", count=batch_size):
        if key not in seen:
            stale.append(key)
            if len(stale) >= batch_size:
                stats["deleted"] += await client.delete(*stale)
                stale.clear()
    if stale:
        stats["deleted"] += await client.delete(*stale)
    await client.set(f"{index}:synced", json.dumps(stats))
    return stats
