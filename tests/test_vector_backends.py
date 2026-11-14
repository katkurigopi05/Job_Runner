"""Synthetic index fidelity and cache lifecycle; Redis integration is opt-in."""

from __future__ import annotations

import os
import struct
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import delete

from packages.core.config import Settings, get_settings
from packages.core.models import Company, Posting
from packages.core.models_chunks import PostingChunk
from packages.matching import chunk_search
from packages.matching.vector_search import Filters, redis_client, redis_filter, search, vector_blob
from packages.matching.vector_sync import sync
from scripts.benchmark_vectors import recall_at_10


def vector(y=0.0):
    return [1.0, y] + [0.0] * 382


def test_backend_is_opt_in_and_validated(monkeypatch):
    monkeypatch.delenv("VECTOR_BACKEND", raising=False)
    assert Settings(_env_file=None).vector_backend == "pgvector"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, vector_backend="unknown")


@pytest.mark.parametrize("bad", [[0.0] * 384, [1.0], [float("nan")] * 384])
def test_bad_vectors_are_refused(bad):
    with pytest.raises(ValueError):
        vector_blob(bad)


def test_float32_and_filter_tags_are_unambiguous():
    assert struct.unpack("<384f", vector_blob(vector()))[:2] == (1.0, 0.0)
    company = uuid.uuid4()
    query = redis_filter("model/with:punctuation", Filters(companies=(company,), remote=False))
    assert f"@company:{{{company.hex}}}" in query
    assert "@remote:{no}" in query and "@open:{yes}" in query
    assert "model/with" not in query


def test_recall_counts_missing_and_duplicate_results():
    ids = [uuid.uuid4() for _ in range(10)]
    assert recall_at_10(ids, ids) == 1.0
    assert recall_at_10(ids, [ids[0]] * 10) == 0.1
    assert recall_at_10(ids, []) == 0.0


@pytest.fixture
async def redis_index():
    if os.environ.get("RUN_REDIS_TESTS") != "1":
        pytest.skip("set RUN_REDIS_TESTS=1 with the optional Redis Stack service")
    pytest.importorskip("redis")
    client = redis_client()
    await client.ping()
    index = f"jobrunner:test:{uuid.uuid4().hex}"
    try:
        yield client, index
    finally:
        try:
            await client.execute_command("FT.DROPINDEX", index, "DD")
        except Exception as exc:
            if "Unknown Index name" not in str(exc):
                raise
        await client.delete(f"{index}:synced")
        await client.aclose()


async def add_corpus(session):
    companies = [Company(name="Synthetic One"), Company(name="Synthetic Two")]
    session.add_all(companies)
    await session.flush()
    chunks = []
    for i in range(12):
        posting = Posting(
            company_id=companies[i % 2].id,
            url=f"https://vectors.invalid/{i}",
            content_hash="current",
            is_remote=i % 3 == 0,
            closed_at=datetime.now(UTC) if i == 10 else None,
        )
        session.add(posting)
        await session.flush()
        chunk = PostingChunk(
            posting_id=posting.id,
            ordinal=0,
            start=0,
            length=20,
            content_hash="stale" if i == 11 else "current",
            embedding_model="other" if i == 9 else "synthetic",
            embedding=vector(i / 10),
        )
        session.add(chunk)
        chunks.append(chunk)
    await session.flush()
    return companies, chunks


async def test_pg_hnsw_respects_current_model_and_filters(db_session):
    companies, _ = await add_corpus(db_session)
    for filters in [Filters(), Filters(remote=True), Filters(companies=(companies[0].id,))]:
        exact = await search(db_session, vector(), "synthetic", backend="pgvector", filters=filters)
        ann = await search(
            db_session, vector(), "synthetic", backend="pgvector_hnsw", filters=filters
        )
        assert [h.chunk_id for h in ann] == [h.chunk_id for h in exact]


async def test_bounded_reranking_ignores_redis_backend(db_session, monkeypatch):
    _, chunks = await add_corpus(db_session)
    monkeypatch.setattr(get_settings(), "vector_backend", "redis_hnsw")
    ranked, every = await chunk_search.chunk_ranking(
        db_session, "synthetic", vector(), among=[chunks[0].posting_id], companies=None, limit=10
    )
    assert ranked == [(chunks[0].posting_id, 0, 20)] and every


async def test_sync_reconciles_updates_and_deletions(committing_sessionmaker, redis_index):
    client, index = redis_index
    async with committing_sessionmaker() as session, session.begin():
        _, chunks = await add_corpus(session)
        chunk_id, posting_id = chunks[0].id, chunks[0].posting_id

    async def run():
        async with committing_sessionmaker() as session, session.begin():
            return await sync(session, client, index=index, batch_size=3)

    first = await run()
    assert first == {"scanned": 11, "written": 11, "deleted": 0}
    assert (await run())["written"] == 0
    async with committing_sessionmaker() as session, session.begin():
        posting = await session.get(Posting, posting_id)
        posting.is_remote = False
        posting.closed_at = datetime.now(UTC)
    assert (await run())["written"] == 1
    doc = await client.hgetall(f"{index}:doc:{chunk_id}")
    assert doc[b"remote"] == b"no" and doc[b"open"] == b"no"
    async with committing_sessionmaker() as session, session.begin():
        await session.execute(delete(PostingChunk).where(PostingChunk.id == chunk_id))
    assert (await run())["deleted"] == 1
    assert not await client.exists(f"{index}:doc:{chunk_id}")


async def test_redis_filters_and_postgres_revalidation(committing_sessionmaker, redis_index):
    client, index = redis_index
    async with committing_sessionmaker() as session, session.begin():
        companies, chunks = await add_corpus(session)
        company_id, posting_id = companies[0].id, chunks[0].posting_id
    async with committing_sessionmaker() as session, session.begin():
        await sync(session, client, index=index)
    async with committing_sessionmaker() as session, session.begin():
        filters = Filters(companies=(company_id,), remote=True)
        expected = await search(session, vector(), "synthetic", backend="pgvector", filters=filters)
        actual = await search(
            session,
            vector(),
            "synthetic",
            backend="redis_hnsw",
            filters=filters,
            client=client,
            index=index,
        )
        assert [h.chunk_id for h in actual] == [h.chunk_id for h in expected]
        posting = await session.get(Posting, posting_id)
        posting.content_hash = "changed-after-sync"
    async with committing_sessionmaker() as session:
        hits = await search(
            session, vector(), "synthetic", backend="redis_hnsw", client=client, index=index
        )
        assert posting_id not in {h.posting_id for h in hits}


async def test_unsynced_redis_fails_visibly(db_session, redis_index):
    client, index = redis_index
    with pytest.raises(RuntimeError, match="not synced"):
        await search(
            db_session, vector(), "synthetic", backend="redis_hnsw", client=client, index=index
        )
