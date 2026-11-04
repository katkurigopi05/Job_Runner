"""Opt-in Qwen chunk search; synthetic vectors, no model or network calls."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete

from apps.api.routers import chat
from packages.core.config import Settings
from packages.core.models import Company, CorpusStats, Posting
from packages.core.models_chunks import PostingChunk, PostingChunkVector
from packages.core.schemas import ChatRequest
from packages.matching import query_vectors
from packages.matching import retrieve as R
from packages.matching.chunk_vectors import QWEN_4B
from packages.matching.embed import SentenceTransformerEmbedder

BGE = SentenceTransformerEmbedder.name


def vector(width, x=1.0, y=0.0):
    return [x, y] + [0.0] * (width - 2)


@pytest.fixture(autouse=True)
def fake_embeddings(monkeypatch):
    calls = []

    async def embed(question, *, keep_alive=None):
        calls.append((question, keep_alive))
        return query_vectors.QueryVector(QWEN_4B, "fake-qwen", tuple(vector(2560)), 0.0)

    async def released():
        pass

    monkeypatch.setattr(query_vectors, "unload_embedder", released, raising=False)
    monkeypatch.setattr(query_vectors, "embed_question", embed)
    monkeypatch.setattr(R, "_encode", lambda *args: {})
    monkeypatch.setattr(R, "_encoders", lambda *args: {})
    monkeypatch.setattr(R, "_encode_chunk_queries", lambda *args: {BGE: vector(384)})
    settings = Settings(_env_file=None, chat_qwen_search=True, search_us_only=False)
    monkeypatch.setattr(R, "get_settings", lambda: settings)
    monkeypatch.setattr(chat, "get_settings", lambda: settings)
    return settings, calls


async def posting(session, title, *, qwen=True, bge=(1.0, 0.0), large=(1.0, 0.0), **kw):
    company = Company(name="Acme", ats_type="greenhouse")
    session.add(company)
    await session.flush()
    p = Posting(
        company_id=company.id,
        title=title,
        url=f"https://jobs.test/{uuid.uuid4()}",
        description_raw="Kafka event streaming pipelines.",
        content_hash="current",
        **kw,
    )
    session.add(p)
    await session.flush()
    c = PostingChunk(
        posting_id=p.id,
        ordinal=0,
        start=0,
        length=31,
        content_hash="current",
        embedding_model=BGE,
        embedding=vector(384, *bge),
    )
    session.add(c)
    await session.flush()
    if qwen:
        session.add(
            PostingChunkVector(
                chunk_id=c.id, embedding_model=QWEN_4B, embedding=vector(2560, *large)
            )
        )
    await session.flush()
    return p, c


def ids(found):
    return [p.posting_id for p in (*found.passages, *found.more)]


async def disagreement(session):
    from datetime import timedelta

    now = datetime.now(UTC)
    bge, _ = await posting(session, "Platform Builder", large=(0.1, 0.9), first_seen_at=now)
    qwen, _ = await posting(
        session, "Platform Builder", bge=(0.1, 0.9), first_seen_at=now - timedelta(days=1)
    )
    await posting(
        session,
        "Platform Builder",
        bge=(0.5, 0.5),
        large=(0.5, 0.5),
        first_seen_at=now - timedelta(days=2),
    )
    return bge, qwen


async def test_off_is_identical_and_never_embeds(db_session, fake_embeddings):
    settings, calls = fake_embeddings
    settings.chat_qwen_search = False
    near, _ = await disagreement(db_session)
    found = await R.retrieve(db_session, "kafka jobs")
    assert found.passages[0].posting_id == near.id
    assert calls == []
    assert found.qwen_searched is None and found.embedding_fallback is None


async def test_qwen_nearest_wins_when_bge_disagrees(db_session, fake_embeddings):
    _, near = await disagreement(db_session)
    db_session.add(CorpusStats(revision=1, total_documents=100, counts_json={"streaming": 90}))
    await db_session.flush()
    found = await R.retrieve(db_session, "streaming jobs")
    assert found.passages[0].posting_id == near.id
    assert fake_embeddings[1] == [("streaming", None)]
    assert found.qwen_searched == 3 and found.bge_fallback_searched == 0


async def test_full_coverage_skips_the_empty_bge_fallback_scans(db_session, fake_embeddings):
    """With Qwen covering every posting, bge-small's "without Qwen" scans can find nothing.

    Each cost about 0.2 s a question on the owner's database (2026-11-03), and
    every posting had Qwen vectors. Coverage is counted once and reused.
    """
    from sqlalchemy import event

    near, _ = await posting(db_session, "Platform Builder")
    db_session.add(CorpusStats(revision=1, total_documents=100, counts_json={"streaming": 90}))
    await db_session.flush()
    statements: list[str] = []
    engine = db_session.bind.sync_engine

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        found = await R.retrieve(db_session, "streaming jobs")
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert ids(found) == [near.id]
    assert found.qwen_searched == 1 and found.bge_fallback_searched == 0
    assert not any("posting_chunks.embedding <=>" in s for s in statements), "no bge-small scan"
    assert sum("qwen_coverage" in s for s in statements) == 1, "coverage counted once"


async def test_uncovered_posting_survives_and_coverage_counts_it(db_session):
    near, _ = await posting(db_session, "Platform Builder")
    newer, _ = await posting(db_session, "Platform Builder", qwen=False)
    session = db_session
    session.add(CorpusStats(revision=1, total_documents=100, counts_json={"streaming": 90}))
    await session.flush()
    found = await R.retrieve(session, "streaming jobs")
    assert ids(found) == [near.id, newer.id]
    assert found.searched == 2 and found.unsearchable == 0
    assert found.qwen_searched == 1 and found.bge_fallback_searched == 1
    context = chat._postings_section(found)
    assert "Qwen3-Embedding-4B: 1" in context and "bge-small: 1" in context


async def test_embedding_error_is_explicit_in_context_and_reply(db_session, monkeypatch):
    p, _ = await posting(db_session, "Platform Builder")

    async def unavailable(*args, **kwargs):
        raise query_vectors.QueryEmbeddingError(
            "Qwen3-Embedding-4B is not pulled. Run ollama pull."
        )

    monkeypatch.setattr(query_vectors, "embed_question", unavailable)
    fake_embeddings = R.get_settings()
    fake_embeddings.chat_semantic_candidates = True
    found = await R.retrieve(db_session, "kafka jobs")
    assert ids(found) == [p.id]
    assert "ollama pull" in found.embedding_fallback
    assert "searched with bge-small:" in chat._postings_section(found)
    ready = chat.Ready(selected="kaggle", include_mail=False, prompt="", found=found)
    reply = chat.reply_for(ready, None, "Results follow.")
    assert "searched with bge-small:" in reply.reply and "ollama pull" in reply.reply


@pytest.mark.parametrize("provider, releases_expected", [("ollama", 1), ("kaggle", 0)])
async def test_provider_decides_embedder_unload(
    db_session, fake_embeddings, monkeypatch, provider, releases_expected
):
    fake_embeddings[0].chat_semantic_candidates = True
    releases = []

    async def release():
        releases.append(True)

    monkeypatch.setattr(query_vectors, "unload_embedder", release)
    await posting(db_session, "Platform Builder")
    await chat.prepare(ChatRequest(message="kafka jobs", provider=provider), db_session)
    assert fake_embeddings[1] == [("kafka jobs", "5m" if provider == "ollama" else None)]
    assert len(releases) == releases_expected


async def test_semantics_never_fills_a_missing_rare_word(db_session, fake_embeddings):
    fake_embeddings[0].chat_semantic_candidates = True
    await posting(db_session, "Platform Builder")
    found = await R.retrieve(db_session, "xyzzynotaword jobs")
    assert found.attempted and ids(found) == []


async def test_semantic_candidates_use_qwen_and_keep_level(db_session, fake_embeddings):
    settings, calls = fake_embeddings
    settings.chat_semantic_candidates = True
    said, _ = await posting(db_session, "Junior Platform Builder", large=(0.2, 0.8))
    unsaid, _ = await posting(db_session, "Junior Systems Builder", bge=(-1.0, 0.0))
    unsaid.description_raw = "Message queues without a named technology."
    senior, _ = await posting(db_session, "Senior Systems Builder")
    senior.description_raw = "Message queues without a named technology."
    await db_session.flush()
    found = await R.retrieve(db_session, "junior kafka jobs")
    assert set(ids(found)) == {said.id, unsaid.id}
    assert [c[0] for c in calls] == ["junior kafka jobs"]


async def test_stale_closed_and_other_model_rows_never_rank(db_session, fake_embeddings):
    fake_embeddings[0].chat_semantic_candidates = True
    kept, _ = await posting(db_session, "Platform Builder")
    stale, c = await posting(db_session, "Platform Builder")
    stale.content_hash = "edited"
    closed, _ = await posting(db_session, "Platform Builder", closed_at=datetime.now(UTC))
    other, c2 = await posting(db_session, "Platform Builder")
    await db_session.execute(delete(PostingChunkVector).where(PostingChunkVector.chunk_id == c2.id))
    db_session.add(
        PostingChunkVector(chunk_id=c2.id, embedding_model="another-model", embedding=vector(2560))
    )
    await db_session.flush()
    found = await R.retrieve(db_session, "kafka jobs")
    assert kept.id in ids(found) and other.id in ids(found)
    assert closed.id not in ids(found)
    assert found.qwen_searched == 1 and found.bge_fallback_searched == 1


def test_setting_is_off_by_default(monkeypatch):
    monkeypatch.delenv("CHAT_QWEN_SEARCH", raising=False)
    assert Settings(_env_file=None).chat_qwen_search is False


async def test_posting_without_chunks_uses_its_bge_posting_vector(db_session, monkeypatch):
    p, c = await posting(db_session, "Platform Builder", qwen=False)
    p.description_embedding = vector(384)
    p.embedding_model = BGE
    await db_session.execute(delete(PostingChunk).where(PostingChunk.id == c.id))
    db_session.add(CorpusStats(revision=1, total_documents=100, counts_json={"streaming": 90}))
    await db_session.flush()
    monkeypatch.setattr(R, "_encode", lambda *args: {(BGE, None): vector(384)})
    found = await R.retrieve(db_session, "streaming jobs")
    assert ids(found) == [p.id]
    assert found.searched == 1 and found.unsearchable == 0
    assert found.qwen_searched == 0 and found.bge_fallback_searched == 1


async def test_rank_one_in_each_space_precedes_rank_two(db_session):
    q1, _ = await posting(db_session, "Platform Builder", large=(1.0, 0.0))
    q2, _ = await posting(db_session, "Platform Builder", large=(0.5, 0.5))
    b1, _ = await posting(db_session, "Platform Builder", qwen=False)
    b2, _ = await posting(db_session, "Platform Builder", qwen=False, bge=(0.5, 0.5))
    db_session.add(CorpusStats(revision=1, total_documents=100, counts_json={"streaming": 90}))
    await db_session.flush()
    found = await R.retrieve(db_session, "streaming jobs")
    assert ids(found) == [q1.id, b1.id, q2.id, b2.id]
    assert found.qwen_searched == 2 and found.bge_fallback_searched == 2


async def test_semantic_embedding_failure_falls_back_for_both_stages(
    db_session, fake_embeddings, monkeypatch
):
    fake_embeddings[0].chat_semantic_candidates = True
    p, _ = await posting(db_session, "Platform Builder")
    original = query_vectors.embed_question

    async def embed(question, **kwargs):
        if question == "kafka jobs":
            raise query_vectors.QueryEmbeddingError("Qwen unavailable for the full question")
        return await original(question, **kwargs)

    monkeypatch.setattr(query_vectors, "embed_question", embed)
    found = await R.retrieve(db_session, "kafka jobs")
    assert ids(found) == [p.id]
    assert found.qwen_searched is None
    assert "full question" in found.embedding_fallback


@pytest.mark.parametrize("constraint", ["company", "area", "place"])
async def test_qwen_semantics_keeps_existing_search_constraints(
    db_session, fake_embeddings, constraint
):
    settings, _ = fake_embeddings
    settings.chat_semantic_candidates = True
    settings.search_us_only = True
    said, _ = await posting(db_session, "Platform Builder", location="San Francisco, CA")
    unsaid, _ = await posting(db_session, "Systems Builder", location="San Francisco, CA")
    unsaid.description_raw = "Message queues without a named technology."
    question = "kafka jobs"
    if constraint == "company":
        company = await db_session.get(Company, unsaid.company_id)
        company.name = "Globex"
        question += " at Acme"
    elif constraint == "area":
        unsaid.location = "London, UK"
    else:
        unsaid.location = "Austin, TX"
        question += " in San Francisco"
    await db_session.flush()
    found = await R.retrieve(db_session, question)
    assert ids(found) == [said.id]


async def test_local_non_search_question_releases_a_warm_query_model(
    db_session, fake_embeddings, monkeypatch
):
    releases = []

    async def release():
        releases.append(True)

    monkeypatch.setattr(query_vectors, "unload_embedder", release, raising=False)
    ready = await chat.prepare(ChatRequest(message="hello", provider="ollama"), db_session)
    assert isinstance(ready, chat.Ready)
    assert not ready.found.attempted and fake_embeddings[1] == []
    assert releases == [True]


async def test_computed_answer_does_not_need_an_ollama_memory_check(db_session, monkeypatch):
    await posting(db_session, "Platform Builder")

    async def unavailable(*args, **kwargs):
        raise query_vectors.QueryEmbeddingError("Qwen3-Embedding-4B is unavailable")

    async def unnecessary():
        raise AssertionError("a computed answer needs no local chat model")

    monkeypatch.setattr(query_vectors, "embed_question", unavailable)
    monkeypatch.setattr(query_vectors, "unload_embedder", unnecessary)
    ready = await chat.prepare(
        ChatRequest(message="how many kafka jobs", provider="ollama"), db_session
    )
    assert isinstance(ready, chat.Ready) and ready.factual_answer is not None
    assert ready.found.embedding_fallback is None, "keyword facts never ask Qwen"


async def test_keyword_order_is_bge_even_when_qwen_disagrees(db_session, fake_embeddings):
    settings, calls = fake_embeddings
    bge, _ = await disagreement(db_session)
    found = await R.retrieve(db_session, "kafka jobs")
    assert found.passages[0].posting_id == bge.id
    assert calls == [], "keyword-only ordering must not load Qwen"
    settings.chat_qwen_search = False
    old = await R.retrieve(db_session, "kafka jobs")
    assert found == old


async def test_the_shortlist_is_shared_across_stages_and_widenings(db_session):
    from sqlalchemy import event

    from packages.matching import chunk_search as CS

    entries = []
    for i in range(6):
        p, c = await posting(db_session, "Platform Builder", large=(1.0, i / 10))
        entries.append((p, c))
    # Repeated chunks and a non-positive match exercise grouping and the cutoff.
    p, _ = entries[0]
    c = PostingChunk(
        posting_id=p.id,
        ordinal=1,
        start=8,
        length=10,
        content_hash="current",
        embedding_model=BGE,
        embedding=vector(384),
    )
    db_session.add(c)
    await db_session.flush()
    db_session.add(
        PostingChunkVector(chunk_id=c.id, embedding_model=QWEN_4B, embedding=vector(2560))
    )
    negative, _ = await posting(db_session, "Platform Builder", large=(-1.0, 0.0))
    await db_session.flush()
    plain = CS.ChunkSpace(QWEN_4B)
    cached = CS.ChunkSpace(QWEN_4B, ranking=CS.ShortlistRanking(vector(2560)))
    original, _ = await CS.chunk_ranking(
        db_session, plain, vector(2560), among=None, companies=None, limit=10000
    )
    original_among, _ = await CS.chunk_ranking(
        db_session, plain, vector(2560), among=[negative.id, p.id], companies=None, limit=10000
    )
    keep = {entries[-1][0].id, entries[-2][0].id}
    old_hits, old_spans = await R._nearest_kept(
        db_session,
        plain,
        vector(2560),
        companies=None,
        want=2,
        counts=lambda h: h.posting_id in keep,
        window=1,
    )
    reads = []
    engine = db_session.bind.sync_engine

    shortlists = []

    def count(conn, cursor, statement, parameters, context, executemany):
        if "posting_chunk_vectors.embedding <=>" in statement:
            reads.append(statement)
        if "<~>" in statement:
            shortlists.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        ranked, every = await CS.chunk_ranking(
            db_session, cached, vector(2560), among=None, companies=None, limit=1
        )
        assert ranked == original and every
        among, _ = await CS.chunk_ranking(
            db_session, cached, vector(2560), among=[negative.id, p.id], companies=None, limit=1
        )
        assert among == original_among
        hits, spans = await R._nearest_kept(
            db_session,
            cached,
            vector(2560),
            companies=None,
            want=2,
            counts=lambda h: h.posting_id in keep,
            window=1,
        )
        assert hits == old_hits and spans == old_spans
        filtered, _ = await CS.chunk_ranking(
            db_session,
            cached,
            vector(2560),
            among=None,
            companies=[entries[-1][0].company_id],
            limit=1,
        )
        assert [row[0] for row in filtered] == [entries[-1][0].id]
        # The 1-bit shortlist replaced the exact scan of every vector
        # (2026-11-03): one shortlist for the question, shared by its stages,
        # and exact distances only for shortlisted, named or company chunks.
        assert len(shortlists) == 1, "one shortlist across all stages and widenings"
        assert reads and all(" IN " in s or "= ANY" in s for s in reads), "only listed chunks"
    finally:
        event.remove(engine, "before_cursor_execute", count)


async def test_two_question_embeddings_release_exactly_once(fake_embeddings, monkeypatch):
    from packages.matching import chunk_search as CS

    releases = []

    async def release():
        releases.append(True)

    monkeypatch.setattr(query_vectors, "unload_embedder", release)
    async with CS.Question(free_embedder=True) as question:
        await question.queries({BGE: vector(384)}, "kafka", enabled=True)
        await question.queries({BGE: vector(384)}, "kafka jobs", enabled=True)
        assert releases == []
    assert fake_embeddings[1] == [("kafka", "5m"), ("kafka jobs", "5m")]
    assert releases == [True]


async def test_release_failure_warns_and_keeps_provider_error(db_session, monkeypatch, capsys):
    from packages.llm.provider import ModelUnreachable

    async def unavailable():
        raise query_vectors.QueryEmbeddingError("Could not release Qwen3-Embedding-4B")

    monkeypatch.setattr(query_vectors, "unload_embedder", unavailable)
    ready = await chat.prepare(ChatRequest(message="hello", provider="ollama"), db_session)
    assert isinstance(ready, chat.Ready)
    assert "query_embedder_release_failed" in capsys.readouterr().out

    class DownProvider:
        async def complete(self, *args, **kwargs):
            raise ModelUnreachable("daemon down")

    monkeypatch.setattr(chat, "provider_for", lambda selected: (DownProvider(), "fake-local"))
    from apps.api.errors import ApiError

    with pytest.raises(ApiError, match="ollama serve") as error:
        await chat.chat(ChatRequest(message="hello", provider="ollama"), db_session)
    assert "Could not release" not in str(error.value)


async def test_semantics_and_judge_share_one_question_scan(
    db_session, fake_embeddings, monkeypatch
):
    from sqlalchemy import event

    fake_embeddings[0].chat_semantic_candidates = True
    await posting(db_session, "Platform Builder")
    unsaid, _ = await posting(db_session, "Systems Builder")
    unsaid.description_raw = "Event streaming without the literal search term."
    await db_session.flush()

    class Judge:
        name = "fake-judge"
        judges = True

        def scores(self, question, passages):
            return [1.0 for _ in passages]

    monkeypatch.setattr(R, "get_reranker", lambda: Judge())
    scans = []
    engine = db_session.bind.sync_engine

    def count(conn, cursor, statement, parameters, context, executemany):
        if "posting_chunk_vectors.embedding <=>" in statement:
            scans.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        found = await R.retrieve(db_session, "kafka jobs")
        assert unsaid.id in ids(found)
        assert len(scans) == 1
        assert fake_embeddings[1] == [("kafka jobs", None)]
        await R.retrieve(db_session, "kafka jobs")
        assert len(scans) == 2, "rankings cannot leak across questions"
    finally:
        event.remove(engine, "before_cursor_execute", count)
