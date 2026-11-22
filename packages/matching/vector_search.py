"""Shared Redis helpers for the optional Qwen indexes. PostgreSQL owns all facts.

Redis stores only derived vectors and filter metadata, never candidate data,
and every distance it returns is re-scored from PostgreSQL. Used by
`qwen_redis` (posting chunks) and `interview.retrieval`. The bge-small chunk
search this module also held, over pgvector, its HNSW index or Redis, went with
bge-small's chunk vectors on 2026-11-22.
"""

from __future__ import annotations

import hashlib
import importlib
import math
import struct
import uuid
from dataclasses import dataclass
from typing import Any

from packages.core.config import get_settings


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


def vector_blob(vector: list[float], *, dim: int) -> bytes:
    if len(vector) != dim or not all(math.isfinite(v) for v in vector):
        raise ValueError(f"expected {dim} finite vector values")
    if not any(vector):
        raise ValueError("cosine search requires a nonzero vector")
    return struct.pack(f"<{dim}f", *vector)


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


async def ensure_redis_index(
    client: Any,
    *,
    index: str,
    dim: int,
    tags: tuple[str, ...] = ("model", "company", "remote", "open"),
) -> None:
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
            *(item for tag in tags for item in (tag, "TAG")),
            "embedding",
            "VECTOR",
            "HNSW",
            10,
            "TYPE",
            "FLOAT32",
            "DIM",
            dim,
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


async def redis_knn(
    client: Any,
    index: str,
    filters: str,
    blob: bytes,
    k: int,
    *,
    field: str,
) -> list[uuid.UUID]:
    raw = await client.execute_command(
        "FT.SEARCH",
        index,
        f"({filters})=>[KNN {k} @embedding $vec "
        f"EF_RUNTIME {max(k, get_settings().vector_ef_search)} AS distance]",
        "PARAMS",
        2,
        "vec",
        blob,
        "SORTBY",
        "distance",
        "RETURN",
        1,
        field,
        "LIMIT",
        0,
        k,
        "DIALECT",
        2,
    )
    return [uuid.UUID(raw[i + 1][1].decode()) for i in range(1, len(raw), 2)]


def redis_filter(model: str, filters: Filters) -> str:
    parts = [f"@model:{{{model_tag(model)}}}"]
    if filters.open_only:
        parts.append("@open:{yes}")
    if filters.remote is not None:
        parts.append("@remote:{yes}" if filters.remote else "@remote:{no}")
    if filters.companies:
        parts.append("@company:{" + "|".join(c.hex for c in filters.companies) + "}")
    return " ".join(parts)


DEFAULT_FILTERS = Filters()
