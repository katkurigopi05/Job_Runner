"""Synthetic index fidelity and cache lifecycle; Redis integration is opt-in."""

from __future__ import annotations

import os
import struct
import uuid

import pytest
from pydantic import ValidationError

from packages.core.config import Settings, get_settings
from packages.core.models import Company, Posting
from packages.core.models_chunks import PostingChunk, PostingChunkVector
from packages.matching import chunk_search
from packages.matching.chunk_vectors import QWEN_4B
from packages.matching.chunks import OVERLAP
from packages.matching.vector_search import Filters, redis_client, redis_filter, vector_blob


def vector(y=0.0):
    return [1.0, y] + [0.0] * 382


def qwen_vector(y=0.0):
    return [1.0, y] + [0.0] * 2558


def test_qwen_backend_is_opt_in_and_validated(monkeypatch):
    monkeypatch.delenv("QWEN_VECTOR_BACKEND", raising=False)
    assert Settings(_env_file=None).qwen_vector_backend == "pgvector"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, qwen_vector_backend="unknown")


def test_the_bge_chunk_backend_setting_is_gone(monkeypatch):
    """VECTOR_BACKEND chose how bge-small's chunk vectors were searched; they are gone."""
    monkeypatch.setenv("VECTOR_BACKEND", "redis_hnsw")
    assert not hasattr(Settings(_env_file=None), "vector_backend")


@pytest.mark.parametrize("bad", [[0.0] * 384, [1.0], [float("nan")] * 384])
def test_bad_vectors_are_refused(bad):
    with pytest.raises(ValueError):
        vector_blob(bad, dim=384)


def test_float32_and_filter_tags_are_unambiguous():
    assert struct.unpack("<384f", vector_blob(vector(), dim=384))[:2] == (1.0, 0.0)
    company = uuid.uuid4()
    query = redis_filter("model/with:punctuation", Filters(companies=(company,), remote=False))
    assert f"@company:{{{company.hex}}}" in query
    assert "@remote:{no}" in query and "@open:{yes}" in query
    assert "model/with" not in query


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


async def test_bounded_reranking_ignores_redis_backend(db_session, monkeypatch):
    """Ranking named postings is exact, so it never asks Redis, set up or not."""
    company = Company(name="Synthetic One")
    db_session.add(company)
    await db_session.flush()
    posting = Posting(
        company_id=company.id, url="https://vectors.invalid/0", content_hash="current"
    )
    db_session.add(posting)
    await db_session.flush()
    chunk = PostingChunk(
        posting_id=posting.id, ordinal=0, start=0, length=20, content_hash="current"
    )
    db_session.add(chunk)
    await db_session.flush()
    db_session.add(
        PostingChunkVector(chunk_id=chunk.id, embedding_model=QWEN_4B, embedding=qwen_vector())
    )
    await db_session.flush()
    monkeypatch.setattr(get_settings(), "qwen_vector_backend", "redis_hnsw")

    ranked, every = await chunk_search.chunk_ranking(
        db_session,
        chunk_search.ChunkSpace(QWEN_4B),
        qwen_vector(),
        among=[posting.id],
        companies=None,
        limit=10,
    )

    assert ranked == [(posting.id, 0, 20)] and every


async def test_qwen_posting_sync_resume_update_delete(db_session, redis_index):
    from sqlalchemy import update

    from packages.matching import qwen_redis

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
