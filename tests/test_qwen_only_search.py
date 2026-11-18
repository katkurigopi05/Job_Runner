"""CHAT_QWEN_ONLY: Qwen3-Embedding-4B is the only model that orders or finds postings.

Synthetic vectors, no model or network calls. With the setting on, bge-small
and the lexical spaces are never loaded or scanned, and a question Qwen
cannot embed is refused with the reason rather than searched another way.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event

from apps.api.errors import ApiError
from apps.api.routers import chat
from packages.core.config import Settings
from packages.core.models import Company, CorpusStats, Posting
from packages.core.models_chunks import PostingChunk, PostingChunkVector
from packages.core.schemas import ChatRequest
from packages.matching import chunk_search, query_vectors
from packages.matching import retrieve as R
from packages.matching.chunk_vectors import QWEN_4B
from packages.matching.embed import SentenceTransformerEmbedder

BGE = SentenceTransformerEmbedder.name
#: What a bge-small scan looks like in SQL: chunk vectors, or a posting's one vector.
BGE_SCANS = ("posting_chunks.embedding <=>", "postings.description_embedding <=>")
OLLAMA_DOWN = "Ollama is not reachable at http://localhost:11434. Start it with `ollama serve`."


def vector(width, x=1.0, y=0.0):
    return [x, y] + [0.0] * (width - 2)


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    calls = []
    settings = Settings(_env_file=None, chat_qwen_only=True, search_us_only=False)

    async def embed(question, *, keep_alive=None):
        calls.append((question, keep_alive))
        return query_vectors.QueryVector(QWEN_4B, "fake-qwen", tuple(vector(2560)), 0.0)

    async def released():
        pass

    def encoders(spaces, *args):
        # Building an encoder is what loads bge-small; strict mode asks for none.
        if settings.chat_qwen_only:
            assert not spaces, f"Qwen-only mode built encoders for {spaces}"
        return {}

    def chunk_queries(encoders, subject):
        return {} if settings.chat_qwen_only else {BGE: vector(384)}

    monkeypatch.setattr(query_vectors, "unload_embedder", released, raising=False)
    monkeypatch.setattr(query_vectors, "embed_question", embed)
    monkeypatch.setattr(R, "_encode", lambda *args: {})
    monkeypatch.setattr(R, "_encoders", encoders)
    monkeypatch.setattr(R, "_encode_chunk_queries", chunk_queries)
    monkeypatch.setattr(R, "get_settings", lambda: settings)
    monkeypatch.setattr(chat, "get_settings", lambda: settings)
    return settings, calls


@pytest.fixture
def ollama_down(monkeypatch):
    async def down(*args, **kwargs):
        raise query_vectors.QueryEmbeddingError(OLLAMA_DOWN)

    monkeypatch.setattr(query_vectors, "embed_question", down)


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
    return p


async def disagreement(session):
    """Three postings saying "kafka": bge-small and Qwen each put a different one nearest."""
    now = datetime.now(UTC)
    bge_near = await posting(session, "Platform Builder", large=(0.1, 0.9), first_seen_at=now)
    qwen_near = await posting(
        session, "Platform Builder", bge=(0.1, 0.9), first_seen_at=now - timedelta(days=1)
    )
    await posting(
        session,
        "Platform Builder",
        bge=(0.5, 0.5),
        large=(0.5, 0.5),
        first_seen_at=now - timedelta(days=2),
    )
    return bge_near, qwen_near


async def common_word(session, word="streaming"):
    """Make the word too common to search for, so the question is answered by vectors."""
    session.add(CorpusStats(revision=1, total_documents=100, counts_json={word: 90}))
    await session.flush()


@contextmanager
def sql(session):
    statements: list[str] = []
    engine = session.bind.sync_engine

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def bge_scanned(statements):
    return any(scan in statement for statement in statements for scan in BGE_SCANS)


def ids(found):
    return [p.posting_id for p in (*found.passages, *found.more)]


def test_setting_is_off_by_default(monkeypatch):
    monkeypatch.delenv("CHAT_QWEN_ONLY", raising=False)
    assert Settings(_env_file=None).chat_qwen_only is False


async def test_keyword_matches_are_ordered_by_qwen_and_bge_is_never_scanned(db_session, fakes):
    settings, calls = fakes
    assert settings.chat_qwen_search is False, "Qwen-only needs no second switch"
    _, qwen_near = await disagreement(db_session)
    with sql(db_session) as statements:
        found = await R.retrieve(db_session, "kafka jobs")
    assert found.passages[0].posting_id == qwen_near.id
    assert calls == [("kafka jobs", None)]
    assert not bge_scanned(statements)
    assert found.qwen_only and found.embedding_fallback is None
    assert found.qwen_searched == 3 and found.qwen_uncovered == 0
    assert found.bge_fallback_searched == 0


async def test_without_qwen_only_the_same_keywords_are_ordered_by_bge(db_session, fakes):
    """The contrast: Qwen search alone leaves keyword order to bge-small."""
    settings, _ = fakes
    settings.chat_qwen_only = False
    settings.chat_qwen_search = True
    bge_near, _ = await disagreement(db_session)
    found = await R.retrieve(db_session, "kafka jobs")
    assert found.passages[0].posting_id == bge_near.id
    assert not found.qwen_only


async def test_a_question_qwen_cannot_embed_is_refused_not_searched_with_bge(
    db_session, ollama_down
):
    await posting(db_session, "Platform Builder")
    with sql(db_session) as statements, pytest.raises(chunk_search.QwenSearchUnavailable) as no:
        await R.retrieve(db_session, "kafka jobs")
    assert "ollama serve" in str(no.value)
    assert not bge_scanned(statements)


async def test_a_vector_only_question_is_refused_too(db_session, ollama_down):
    await posting(db_session, "Platform Builder")
    await common_word(db_session)
    with pytest.raises(chunk_search.QwenSearchUnavailable):
        await R.retrieve(db_session, "streaming jobs")


@pytest.mark.parametrize("provider", ["kaggle", "ollama"])
async def test_chat_names_the_model_the_reason_and_that_nothing_fell_back(
    db_session, ollama_down, provider
):
    await posting(db_session, "Platform Builder")
    with pytest.raises(ApiError) as refused:
        await chat.prepare(ChatRequest(message="kafka jobs", provider=provider), db_session)
    message = refused.value.message
    assert "Qwen3-Embedding-4B" in message and "CHAT_QWEN_ONLY" in message
    assert "ollama serve" in message
    assert "bge-small" in message, "says the search did not fall back"


async def test_a_question_that_needs_no_search_is_answered_with_ollama_down(
    db_session, ollama_down
):
    await posting(db_session, "Platform Builder")
    found = await R.retrieve(db_session, "hello")
    assert not found.attempted


async def test_a_keyword_match_without_qwen_vectors_is_listed_and_counted(db_session):
    covered = await posting(db_session, "Platform Builder")
    uncovered = await posting(db_session, "Platform Builder", qwen=False)
    found = await R.retrieve(db_session, "kafka jobs")
    assert set(ids(found)) == {covered.id, uncovered.id}, "keywords still decide relevance"
    assert found.qwen_searched == 1 and found.qwen_uncovered == 1
    assert found.bge_fallback_searched == 0
    context = chat._postings_section(found)
    assert "Qwen3-Embedding-4B only: 1 open postings" in context
    assert "1 have no Qwen vectors yet" in context
    assert "without Qwen vectors" not in context, "no bge-small coverage claimed"


async def test_a_vector_only_question_reaches_only_postings_with_qwen_vectors(db_session):
    covered = await posting(db_session, "Platform Builder")
    await posting(db_session, "Platform Builder", qwen=False)
    await common_word(db_session)
    with sql(db_session) as statements:
        found = await R.retrieve(db_session, "streaming jobs")
    assert ids(found) == [covered.id]
    assert found.searched == 1 and found.unsearchable == 1
    assert found.qwen_searched == 1 and found.qwen_uncovered == 1
    assert not bge_scanned(statements)


async def test_semantic_candidates_share_the_one_qwen_vector(db_session, fakes):
    settings, calls = fakes
    settings.chat_semantic_candidates = True
    said = await posting(db_session, "Platform Builder")
    unsaid = await posting(db_session, "Systems Builder", large=(0.9, 0.1))
    unsaid.description_raw = "Message queues without a named technology."
    await db_session.flush()
    with sql(db_session) as statements:
        found = await R.retrieve(db_session, "kafka jobs")
    assert set(ids(found)) == {said.id, unsaid.id}
    assert calls == [("kafka jobs", None)], "one embedding orders and discovers"
    assert not bge_scanned(statements)


@pytest.mark.parametrize("provider, releases_expected", [("ollama", 1), ("kaggle", 0)])
async def test_local_answers_still_release_the_query_model(
    db_session, fakes, monkeypatch, provider, releases_expected
):
    releases = []

    async def release():
        releases.append(True)

    monkeypatch.setattr(query_vectors, "unload_embedder", release)
    await posting(db_session, "Platform Builder")
    await chat.prepare(ChatRequest(message="kafka jobs", provider=provider), db_session)
    assert fakes[1] == [("kafka jobs", "5m" if provider == "ollama" else None)]
    assert len(releases) == releases_expected


async def test_a_company_question_is_refused_too_since_qwen_orders_it(db_session, ollama_down):
    """Strict means strict: the keywords matched, but the order shown is Qwen's."""
    await posting(db_session, "Platform Builder")
    with pytest.raises(chunk_search.QwenSearchUnavailable):
        await R.retrieve(db_session, "jobs at Acme")


@pytest.mark.parametrize("route", ["/chat", "/chat/stream"])
async def test_both_routes_refuse_with_the_same_error_before_any_answer(client, monkeypatch, route):
    async def refused(*args, **kwargs):
        raise chunk_search.QwenSearchUnavailable(OLLAMA_DOWN)

    monkeypatch.setattr(chat, "retrieve", refused)
    answered = await client.post(route, json={"message": "kafka jobs", "provider": "kaggle"})
    assert answered.status_code == 500
    message = answered.json()["error"]["message"]
    assert "CHAT_QWEN_ONLY" in message and "ollama serve" in message
