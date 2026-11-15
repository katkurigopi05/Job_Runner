"""Offline unit tests for vector backend routing and RediSearch query helpers."""

from __future__ import annotations

import math
import struct
import uuid

import pytest

from packages.core.models import EMBEDDING_DIM
from packages.matching.vector_search import (
    Filters,
    ensure_redis_index,
    model_tag,
    redis_filter,
    search,
    vector_blob,
)


def test_vector_blob_packs_float32_and_rejects_bad_vectors() -> None:
    vec = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
    blob = vector_blob(vec)
    assert len(blob) == EMBEDDING_DIM * 4
    unpacked = struct.unpack(f"<{EMBEDDING_DIM}f", blob)
    assert unpacked[0] == pytest.approx(1.0)

    with pytest.raises(ValueError, match="nonzero vector"):
        vector_blob([0.0] * EMBEDDING_DIM)

    with pytest.raises(ValueError, match="finite"):
        vector_blob([math.nan] + [0.0] * (EMBEDDING_DIM - 1))

    with pytest.raises(ValueError, match=str(EMBEDDING_DIM)):
        vector_blob([1.0, 2.0])


def test_redis_filter_encodes_model_open_remote_and_companies() -> None:
    cid = uuid.UUID("11111111-1111-1111-1111-111111111111")
    query = redis_filter(
        "BAAI/bge-small-en-v1.5",
        Filters(companies=(cid,), remote=True, open_only=True),
    )
    assert f"@model:{{{model_tag('BAAI/bge-small-en-v1.5')}}}" in query
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
    await ensure_redis_index(client, index="jobrunner:test:v1")
    await ensure_redis_index(client, index="jobrunner:test:v1")
    assert calls[0][:4] == ("FT.CREATE", "jobrunner:test:v1", "ON", "HASH")


@pytest.mark.asyncio
async def test_redis_search_fails_visibly_when_not_synced() -> None:
    class FakeSession:
        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("Postgres should not run before Redis synced check")

    class FakeRedis:
        async def get(self, _key: str) -> bytes | None:
            return None

    with pytest.raises(RuntimeError, match="not synced"):
        await search(
            FakeSession(),  # type: ignore[arg-type]
            [1.0] + [0.0] * (EMBEDDING_DIM - 1),
            "BAAI/bge-small-en-v1.5",
            backend="redis_hnsw",
            client=FakeRedis(),
            index="jobrunner:test:v1",
        )


@pytest.mark.asyncio
async def test_redis_search_executes_knn_and_revalidates_in_postgres() -> None:
    chunk_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
    posting_id = uuid.UUID("33333333-3333-3333-3333-333333333333")
    seen_commands: list[tuple[object, ...]] = []

    class FakeResult:
        def all(self) -> list[tuple[uuid.UUID, uuid.UUID, int, int, float]]:
            return [(chunk_id, posting_id, 0, 120, 0.12)]

    class FakeSession:
        async def execute(self, *_args: object, **_kwargs: object) -> FakeResult:
            return FakeResult()

    class FakeRedis:
        async def get(self, _key: str) -> bytes:
            return b'{"written": 1}'

        async def execute_command(self, *args: object) -> list[object]:
            seen_commands.append(args)
            return [1, b"doc:1", [b"chunk_id", str(chunk_id).encode()]]

    hits = await search(
        FakeSession(),  # type: ignore[arg-type]
        [1.0] + [0.0] * (EMBEDDING_DIM - 1),
        "BAAI/bge-small-en-v1.5",
        backend="redis_hnsw",
        k=5,
        client=FakeRedis(),
        index="jobrunner:test:v1",
    )

    assert len(hits) == 1
    assert hits[0].chunk_id == chunk_id
    assert hits[0].posting_id == posting_id
    assert hits[0].distance == pytest.approx(0.12)
    assert seen_commands[0][0] == "FT.SEARCH"
    assert "KNN 5 @embedding $vec" in str(seen_commands[0][2])
