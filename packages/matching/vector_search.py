"""Optional chunk accelerators. PostgreSQL owns all facts and final distances.

Redis stores only derived vectors and filter metadata, never candidate data.
Named/keyword candidate re-ranking and the 2,560-wide Qwen path stay exact.
"""

from __future__ import annotations

import hashlib
import importlib
import math
import struct
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.config import get_settings
from packages.core.models import EMBEDDING_DIM, Posting
from packages.core.models_chunks import PostingChunk

Backend = Literal["pgvector", "pgvector_hnsw", "redis_hnsw"]
INDEX = "jobrunner:chunks:v1"
PREFIX = f"{INDEX}:doc:"


@dataclass(frozen=True)
class Filters:
    companies: tuple[uuid.UUID, ...] = ()
    remote: bool | None = None
    open_only: bool = True


@dataclass(frozen=True)
class Hit:
    chunk_id: uuid.UUID
    posting_id: uuid.UUID
    start: int
    length: int
    distance: float


def vector_blob(vector: list[float]) -> bytes:
    if len(vector) != EMBEDDING_DIM or not all(math.isfinite(v) for v in vector):
        raise ValueError(f"expected {EMBEDDING_DIM} finite vector values")
    if not any(vector):
        raise ValueError("cosine search requires a nonzero vector")
    return struct.pack(f"<{EMBEDDING_DIM}f", *vector)


def model_tag(model: str) -> str:
    # Hex tags avoid RediSearch's punctuation/token escaping rules.
    return hashlib.sha256(model.encode()).hexdigest()


def redis_client() -> Any:
    try:
        redis = importlib.import_module("redis.asyncio")
    except ModuleNotFoundError as exc:
        raise RuntimeError('Install the optional backend: pip install -e ".[redis]"') from exc
    return redis.from_url(
        get_settings().redis_url,
        decode_responses=False,
        socket_connect_timeout=3,
        socket_timeout=10,
    )


async def ensure_redis_index(client: Any, *, index: str = INDEX) -> None:
    try:
        await client.execute_command(
            "FT.CREATE",
            index,
            "ON",
            "HASH",
            "PREFIX",
            1,
            f"{index}:doc:",
            "SCHEMA",
            "model",
            "TAG",
            "company",
            "TAG",
            "remote",
            "TAG",
            "open",
            "TAG",
            "embedding",
            "VECTOR",
            "HNSW",
            10,
            "TYPE",
            "FLOAT32",
            "DIM",
            EMBEDDING_DIM,
            "DISTANCE_METRIC",
            "COSINE",
            "M",
            16,
            "EF_CONSTRUCTION",
            200,
        )
    except Exception as exc:
        if "Index already exists" not in str(exc):
            raise


def redis_filter(model: str, filters: Filters) -> str:
    parts = [f"@model:{{{model_tag(model)}}}"]
    if filters.open_only:
        parts.append("@open:{yes}")
    if filters.remote is not None:
        parts.append("@remote:{yes}" if filters.remote else "@remote:{no}")
    if filters.companies:
        parts.append("@company:{" + "|".join(c.hex for c in filters.companies) + "}")
    return " ".join(parts)


def predicates(model: str, filters: Filters) -> list[Any]:
    clauses = [
        PostingChunk.embedding_model == model,
        PostingChunk.content_hash.is_not_distinct_from(Posting.content_hash),
    ]
    if filters.open_only:
        clauses.append(Posting.closed_at.is_(None))
    if filters.remote is not None:
        clauses.append(Posting.is_remote.is_(filters.remote))
    if filters.companies:
        clauses.append(Posting.company_id.in_(filters.companies))
    return clauses


DEFAULT_FILTERS = Filters()


async def search(
    session: AsyncSession,
    vector: list[float],
    model: str,
    *,
    backend: Backend,
    k: int = 10,
    filters: Filters = DEFAULT_FILTERS,
    client: Any = None,
    index: str = INDEX,
) -> list[Hit]:
    """Top chunks (not distinct postings), with live PostgreSQL validation.

    Exact order uses distance + 0 to prevent matching an ANN index's ORDER BY.
    Redis candidates are re-scored from the source halfvec, making all three
    engines compare the same representation. Missing/stale caches fail visibly.
    """
    vector_blob(vector)
    # PostgreSQL casts the query to halfvec too. Give Redis that same query
    # representation so recall measures the index, not quantization drift.
    try:
        vector = list(
            struct.unpack(f"<{EMBEDDING_DIM}e", struct.pack(f"<{EMBEDDING_DIM}e", *vector))
        )
    except (OverflowError, struct.error) as exc:
        raise ValueError("vector values exceed halfvec range") from exc
    blob = vector_blob(vector)
    if k < 1:
        raise ValueError("k must be positive")
    if backend not in ("pgvector", "pgvector_hnsw", "redis_hnsw"):
        raise ValueError("unknown vector backend")
    distance = PostingChunk.embedding.cosine_distance(vector)
    query = (
        select(
            PostingChunk.id,
            PostingChunk.posting_id,
            PostingChunk.start,
            PostingChunk.length,
            distance,
        )
        .join(Posting, Posting.id == PostingChunk.posting_id)
        .where(*predicates(model, filters))
    )
    if backend == "redis_hnsw":
        owned = client is None
        client = client if client is not None else redis_client()
        try:
            if not await client.get(f"{index}:synced"):
                raise RuntimeError("Redis chunks are not synced; run make sync-vectors")
            raw = await client.execute_command(
                "FT.SEARCH",
                index,
                f"({redis_filter(model, filters)})=>[KNN {k} @embedding $vec "
                f"EF_RUNTIME {max(k, get_settings().vector_ef_search)} AS distance]",
                "PARAMS",
                2,
                "vec",
                blob,
                "SORTBY",
                "distance",
                "RETURN",
                1,
                "chunk_id",
                "LIMIT",
                0,
                k,
                "DIALECT",
                2,
            )
            ids = [uuid.UUID(raw[i + 1][1].decode()) for i in range(1, len(raw), 2)]
        finally:
            if owned:
                await client.aclose()
        if not ids:
            return []
        query = query.where(PostingChunk.id.in_(ids))
    elif backend == "pgvector_hnsw":
        # Keep metadata joins outside KNN; otherwise the planner can sort a
        # joined relation and never visit HNSW. Widen for selective filters.
        await session.execute(
            text("SELECT set_config('hnsw.ef_search', :ef, true)"),
            {"ef": str(min(1000, max(k, get_settings().vector_ef_search)))},
        )
        await session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
        size = max(k * 4, 100)
        while True:
            candidates = list(
                (
                    await session.scalars(
                        select(PostingChunk.id)
                        .where(PostingChunk.embedding_model == model)
                        .order_by(distance)
                        .limit(size)
                    )
                ).all()
            )
            if not candidates:
                return []
            rows = (await session.execute(query.where(PostingChunk.id.in_(candidates)))).all()
            if len(rows) >= k or len(candidates) < size:
                break
            size *= 2
        hits = [
            Hit(cid, pid, start, length, float(dist))
            for cid, pid, start, length, dist in rows
            if dist is not None and math.isfinite(float(dist))
        ]
        return sorted(hits, key=lambda h: (h.distance, str(h.chunk_id)))[:k]
    else:
        query = query.order_by(distance + 0.0, PostingChunk.id).limit(k)
    rows = (await session.execute(query)).all()
    hits = [
        Hit(cid, pid, start, length, float(dist))
        for cid, pid, start, length, dist in rows
        if dist is not None and math.isfinite(float(dist))
    ]
    return sorted(hits, key=lambda h: (h.distance, str(h.chunk_id)))[:k]
