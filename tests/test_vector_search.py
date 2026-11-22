"""Offline unit tests for the RediSearch helpers the Qwen indexes share.

The bge-small chunk search these also covered went with bge-small's chunk
vectors on 2026-11-22; `qwen_redis` and interview evidence use what is left.
"""

from __future__ import annotations

import math
import struct
import uuid

import pytest

from packages.core.models_chunks import LARGE_EMBEDDING_DIM
from packages.matching.chunk_vectors import QWEN_4B
from packages.matching.vector_search import (
    Filters,
    ensure_redis_index,
    model_tag,
    redis_filter,
    redis_knn,
    vector_blob,
)

EMBEDDING_DIM = LARGE_EMBEDDING_DIM


def test_vector_blob_packs_float32_and_rejects_bad_vectors() -> None:
    vec = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
    blob = vector_blob(vec, dim=EMBEDDING_DIM)
    assert len(blob) == EMBEDDING_DIM * 4
    unpacked = struct.unpack(f"<{EMBEDDING_DIM}f", blob)
    assert unpacked[0] == pytest.approx(1.0)

    with pytest.raises(ValueError, match="nonzero vector"):
        vector_blob([0.0] * EMBEDDING_DIM, dim=EMBEDDING_DIM)

    with pytest.raises(ValueError, match="finite"):
        vector_blob([math.nan] + [0.0] * (EMBEDDING_DIM - 1), dim=EMBEDDING_DIM)

    with pytest.raises(ValueError, match=str(EMBEDDING_DIM)):
        vector_blob([1.0, 2.0], dim=EMBEDDING_DIM)


def test_redis_filter_encodes_model_open_remote_and_companies() -> None:
    cid = uuid.UUID("11111111-1111-1111-1111-111111111111")
    query = redis_filter(QWEN_4B, Filters(companies=(cid,), remote=True, open_only=True))
    assert f"@model:{{{model_tag(QWEN_4B)}}}" in query
    assert "@open:{yes}" in query
    assert "@remote:{yes}" in query
    assert f"@company:{{{cid.hex}}}" in query


@pytest.mark.asyncio
async def test_ensure_redis_index_ignores_existing_index() -> None:
    calls: list[tuple[object, ...]] = []

    class FakeRedis:
        async def execute_command(self, *args: object) -> None:
            calls.append(args)
            if len(calls) > 1:
                raise RuntimeError("Index already exists")

    client = FakeRedis()
    await ensure_redis_index(client, index="jobrunner:test:v1", dim=EMBEDDING_DIM)
    await ensure_redis_index(client, index="jobrunner:test:v1", dim=EMBEDDING_DIM)
    assert calls[0][:4] == ("FT.CREATE", "jobrunner:test:v1", "ON", "HASH")
    assert EMBEDDING_DIM in calls[0]


@pytest.mark.asyncio
async def test_redis_knn_asks_for_k_neighbours_and_reads_back_their_ids() -> None:
    chunk_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
    seen_commands: list[tuple[object, ...]] = []

    class FakeRedis:
        async def execute_command(self, *args: object) -> list[object]:
            seen_commands.append(args)
            return [1, b"doc:1", [b"chunk_id", str(chunk_id).encode()]]

    blob = vector_blob([1.0] + [0.0] * (EMBEDDING_DIM - 1), dim=EMBEDDING_DIM)
    ids = await redis_knn(
        FakeRedis(), "jobrunner:test:v1", "@open:{yes}", blob, 5, field="chunk_id"
    )

    assert ids == [chunk_id]
    assert seen_commands[0][0] == "FT.SEARCH"
    assert "KNN 5 @embedding $vec" in str(seen_commands[0][2])
