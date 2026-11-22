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


async def test_qwen_posting_sync_resume_update_delete(db_session, redis_index):
    from sqlalchemy import update

    from packages.core.models_chunks import PostingChunkVector
    from packages.matching import qwen_redis
    from packages.matching.chunk_vectors import QWEN_4B
    from packages.matching.chunks import OVERLAP

    client, index = redis_index
    company = Company(name="Qwen cache benchmark fixture")
    db_session.add(company)
    await db_session.flush()
    posting = Posting(
        url="https://vector.example.com/qwen", company_id=company.id, content_hash="x"
    )
    db_session.add(posting)
    await db_session.flush()
    chunks = [
        PostingChunk(
            posting_id=posting.id,
            ordinal=index_,
            start=index_ * 20,
            length=20,
            content_hash="x",
            overlap=OVERLAP,
            embedding_model="fixture-small",
            embedding=vector(),
        )
        for index_ in range(3)
    ]
    db_session.add_all(chunks)
    await db_session.flush()
    values = [[1.0, y] + [0.0] * 2558 for y in (0.0, 0.1, 0.4)]
    db_session.add_all(
        [
            PostingChunkVector(chunk_id=chunk.id, embedding_model=QWEN_4B, embedding=embedding)
            for chunk, embedding in zip(chunks, values, strict=True)
        ]
    )
    await db_session.flush()
    one = await qwen_redis.sync(db_session, client, index=index, batch_size=1, max_batches=1)
    assert one["scanned"] == one["written"] == 1 and not one["complete"]
    two = await qwen_redis.sync(db_session, client, index=index, batch_size=1)
    assert two["scanned"] == two["written"] == 2 and two["complete"]
    kwargs = {"filters": Filters(companies=(company.id,)), "client": client, "index": index}
    exact = await qwen_redis.search(db_session, values[0], **kwargs)
    approx = await qwen_redis.search(db_session, values[0], redis=True, **kwargs)
    assert exact == approx and len(approx) == 3

    await db_session.execute(update(Posting).where(Posting.id == posting.id).values(is_remote=True))
    synced = await qwen_redis.sync(db_session, client, index=index)
    assert synced["written"] == 3
    await db_session.delete(chunks[0])
    await db_session.flush()
    stale = await qwen_redis.search(db_session, values[0], redis=True, **kwargs)
    assert len(stale) == 2
    assert (await qwen_redis.sync(db_session, client, index=index))["deleted"] == 1
    result = await qwen_redis.sync(db_session, client, index=index, rebuild=True)
    assert result["complete"] and result["written"] == 2


async def test_qwen_evidence_filters_and_deletion(db_session, redis_index):
    from packages.core.models import Application, Candidate, Profile, User
    from packages.core.models_interview import InterviewPlan, InterviewRound, InterviewVector
    from packages.interview import retrieval
    from packages.matching.chunk_vectors import QWEN_4B

    client, index = redis_index
    user = User(email="qwen-cache@example.com")
    db_session.add(user)
    await db_session.flush()
    candidate = Candidate(user_id=user.id, name="Cache fixture", email="cache@example.com")
    company = Company(name="Qwen evidence fixture")
    db_session.add_all([candidate, company])
    await db_session.flush()
    profile = Profile(candidate_id=candidate.id, label="Fixture")
    posting = Posting(url="https://cache.example.com/job", company_id=company.id)
    db_session.add_all([profile, posting])
    await db_session.flush()
    apps = [
        Application(
            candidate_id=candidate.id,
            profile_id=profile.id,
            posting_id=posting.id,
            url=f"https://cache.example.com/{number}",
        )
        for number in range(2)
    ]
    db_session.add_all(apps)
    await db_session.flush()
    rounds = [InterviewRound(application_id=app.id) for app in (apps[0], apps[0], apps[1])]
    db_session.add_all(rounds)
    await db_session.flush()
    vector = [1.0] + [0.0] * 2559
    for number, round_ in enumerate(rounds):
        plan = InterviewPlan(
            round_id=round_.id,
            request_id=uuid.uuid4(),
            inputs={},
            progress={},
            status="local",
        )
        db_session.add(plan)
        await db_session.flush()
        db_session.add(
            InterviewVector(
                id=uuid.uuid4(),
                plan_id=plan.id,
                source_id=f"source-{number}",
                kind="project",
                model=QWEN_4B,
                embedding=vector,
            )
        )
    await db_session.flush()
    batches = await retrieval.sync(db_session, client, index=index, batch_size=1, max_batches=2)
    assert batches["scanned"] == 2 and not batches["complete"]
    finished = await retrieval.sync(db_session, client, index=index)
    assert finished["scanned"] == 1 and finished["complete"]
    kwargs = {"application_id": apps[0].id, "client": client, "index": index}
    for use_redis in (False, True):
        visible = await retrieval.search(db_session, vector, redis=use_redis, **kwargs)
        assert {source for _, source in visible} == {"source-0", "source-1"}
        scoped = await retrieval.search(
            db_session,
            vector,
            redis=use_redis,
            **kwargs,
            round_id=rounds[1].id,
            company_id=company.id,
        )
        assert [source for _, source in scoped] == ["source-1"]
    await db_session.delete(rounds[1])
    await db_session.flush()
    assert len(await retrieval.search(db_session, vector, redis=True, **kwargs)) == 1
    assert (await retrieval.sync(db_session, client, index=index))["deleted"] == 1
