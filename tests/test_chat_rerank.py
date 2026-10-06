"""A second model re-orders what the assistant's search found.

Measured on the owner's corpus on 2026-10-05 (56 postings, two questions each,
1,000-posting pool): bge-small alone put the posting a question was written
from in the top five for 64% of natural questions and 11% of paraphrased ones.
Letting Qwen3-Embedding-0.6B re-score the best chunk of the top 30 candidates
took that to 77% and 36% — nearly what re-embedding the whole corpus with it
gets (80% and 34%), with nothing to backfill and about 1.6 s per question.

So the re-ranker only ever re-orders. It never adds a posting the search did
not find, which keeps §14's rule that keywords decide relevance, and it is off
unless a model is named, so a machine without it searches exactly as before.

No test here loads a model: the re-ranker is a fake that scores by a word.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from packages.core.config import Settings
from packages.core.models import Company, CorpusStats, Posting
from packages.matching import rerank
from packages.matching import retrieve as R
from packages.matching.chunks import chunk_postings
from packages.matching.embed import LexicalEmbedder
from packages.matching.retrieve import retrieve

LEXICAL = LexicalEmbedder()
FILLER = "Our team values ownership and curiosity in everything we ship. " * 40


class WordReranker:
    """Scores a passage by how often it says one word. Records what it was asked."""

    name = "fake-reranker"

    def __init__(self, word: str) -> None:
        self.word = word
        self.calls: list[tuple[str, list[str]]] = []

    def scores(self, question: str, passages: list[str]) -> list[float]:
        self.calls.append((question, passages))
        return [float(passage.lower().count(self.word)) for passage in passages]


class BrokenReranker:
    name = "broken"

    def scores(self, question: str, passages: list[str]) -> list[float]:
        raise RuntimeError("model fell over")


@pytest.fixture
def use(monkeypatch):
    def install(reranker) -> None:
        monkeypatch.setattr(R, "get_reranker", lambda: reranker)

    return install


async def _company(session, name: str = "Acme") -> Company:
    company = Company(name=name, ats_type="greenhouse")
    session.add(company)
    await session.flush()
    return company


async def _posting(session, company, title, body, *, days_ago=0) -> Posting:
    posting = Posting(
        company_id=company.id,
        external_id=uuid.uuid4().hex[:8],
        url=f"https://x.test/{uuid.uuid4().hex[:8]}",
        title=title,
        description_raw=body,
        content_hash="h1",
        first_seen_at=datetime.now(UTC) - timedelta(days=days_ago),
    )
    session.add(posting)
    await session.flush()
    return posting


async def _kafka_three(session):
    """Three postings the keyword scan ranks C, B, A (newest first)."""
    company = await _company(session)
    a = await _posting(session, company, "A", "Kafka pipelines. Streaming streaming.", days_ago=3)
    b = await _posting(session, company, "B", "Kafka pipelines. Streaming.", days_ago=2)
    c = await _posting(session, company, "C", "Kafka pipelines.", days_ago=1)
    return a, b, c


# --- off unless asked for ----------------------------------------------------------


def test_no_model_is_named_by_default() -> None:
    """A fresh checkout must not download 1.2 GB or load a model to answer a question."""
    assert Settings(_env_file=None).chat_rerank_model == ""


def test_without_a_named_model_there_is_no_reranker(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_RERANK_MODEL", "")
    rerank.get_reranker.cache_clear()
    try:
        assert rerank.get_reranker() is None
    finally:
        rerank.get_reranker.cache_clear()


async def test_search_is_unchanged_without_a_reranker(db_session, use) -> None:
    a, b, c = await _kafka_three(db_session)
    use(None)

    found = await retrieve(db_session, "kafka roles")

    assert [p.posting_id for p in found.passages] == [c.id, b.id, a.id]
    assert found.reranked_by is None


# --- re-ordering -------------------------------------------------------------------


async def test_the_reranker_reorders_what_the_search_found(db_session, use) -> None:
    a, b, c = await _kafka_three(db_session)
    fake = WordReranker("streaming")
    use(fake)

    found = await retrieve(db_session, "kafka roles")

    assert [p.posting_id for p in found.passages] == [a.id, b.id, c.id]
    assert found.reranked_by == "fake-reranker"


async def test_it_is_asked_the_owners_question_about_the_postings_text(db_session, use) -> None:
    await _kafka_three(db_session)
    fake = WordReranker("streaming")
    use(fake)

    await retrieve(db_session, "kafka roles")

    ((question, passages),) = fake.calls
    assert question == "kafka roles"
    assert len(passages) == 3
    assert all("kafka" in passage.lower() for passage in passages)


async def test_it_never_adds_a_posting_the_search_did_not_find(db_session, use) -> None:
    """Keywords decide relevance (§14); the re-ranker only orders the matches."""
    a, b, c = await _kafka_three(db_session)
    company = await _company(db_session, "Other")
    await _posting(db_session, company, "Chef", "Streaming streaming streaming streaming.")
    use(WordReranker("streaming"))

    found = await retrieve(db_session, "kafka roles")

    assert {p.posting_id for p in found.passages} == {a.id, b.id, c.id}


async def test_a_posting_asked_for_by_title_stays_first(db_session, use) -> None:
    """§14: a posting whose whole title is in the question goes first."""
    company = await _company(db_session)
    exact = await _posting(db_session, company, "Forward Deployed Engineer", "Customer work.")
    other = await _posting(
        db_session, company, "Engineer", "Forward deployed engineer streaming streaming."
    )
    use(WordReranker("streaming"))

    found = await retrieve(db_session, "Forward Deployed Engineer")

    assert [p.posting_id for p in found.passages][:2] == [exact.id, other.id]


async def test_only_the_top_of_the_list_is_reordered(db_session, use, monkeypatch) -> None:
    """A bounded pool: the cost is per candidate, and 100 was no better than 30."""
    a, b, c = await _kafka_three(db_session)
    monkeypatch.setattr(R, "_rerank_pool", lambda: 2)
    fake = WordReranker("streaming")
    use(fake)

    found = await retrieve(db_session, "kafka roles")

    # C and B are the top two and swap; A, third, is not looked at.
    assert [p.posting_id for p in found.passages] == [b.id, c.id, a.id]
    assert len(fake.calls[0][1]) == 2


async def test_a_reranker_that_fails_leaves_the_search_order(db_session, use) -> None:
    """The assistant must still answer when the second model cannot."""
    a, b, c = await _kafka_three(db_session)
    use(BrokenReranker())

    found = await retrieve(db_session, "kafka roles")

    assert [p.posting_id for p in found.passages] == [c.id, b.id, a.id]
    assert found.reranked_by is None


async def test_it_reorders_a_search_answered_by_vectors_alone(db_session, use) -> None:
    """No distinguishing word, so chunks answer; the best chunk is what is re-scored."""
    db_session.add(
        CorpusStats(revision=1, total_documents=100, counts_json={"data": 90, "engineering": 90})
    )
    company = await _company(db_session)
    plain = await _posting(
        db_session, company, "Engineer", FILLER + " You will own data engineering for payments."
    )
    richer = await _posting(
        db_session,
        company,
        "Analyst",
        FILLER + " Data engineering with streaming streaming.",
        days_ago=1,
    )
    await chunk_postings(db_session, embedder=LEXICAL)
    fake = WordReranker("streaming")
    use(fake)

    found = await retrieve(db_session, "any data engineering roles?")

    assert found.passages[0].posting_id == richer.id
    assert {p.posting_id for p in found.passages} == {plain.id, richer.id}
    assert any("streaming" in passage for passage in fake.calls[0][1]), "the matching chunk"


# --- the ordering itself -----------------------------------------------------------


def test_reorder_is_stable_for_equal_scores() -> None:
    """Ties keep the search's order, so the re-ranker cannot shuffle on noise."""
    assert rerank.reorder(["a", "b", "c", "d"], [1.0, 3.0, 1.0, 3.0]) == ["b", "d", "a", "c"]


def test_reorder_refuses_a_score_list_of_the_wrong_length() -> None:
    with pytest.raises(ValueError, match="2 scores for 3"):
        rerank.reorder(["a", "b", "c"], [1.0, 2.0])


async def test_a_question_that_only_names_a_company_is_not_reranked(db_session, use) -> None:
    """ "jobs at Astranis" says nothing to order by.

    Seen on the owner's database on 2026-10-05: the re-ranker reshuffled
    Astranis's postings by how much each resembled the words "jobs at
    Astranis", and added 1.5 s to do it.
    """
    company = await _company(db_session, "Hightouch")
    newer = await _posting(db_session, company, "Engineer", "Streaming streaming.", days_ago=1)
    older = await _posting(db_session, company, "Designer", "Streaming.", days_ago=2)
    fake = WordReranker("designer")
    use(fake)

    found = await retrieve(db_session, "jobs at Hightouch")

    assert fake.calls == []
    assert found.reranked_by is None
    assert {p.posting_id for p in found.passages} == {newer.id, older.id}


async def test_a_company_question_that_says_more_is_reranked(db_session, use) -> None:
    company = await _company(db_session, "Hightouch")
    await _posting(db_session, company, "Engineer", "Kafka pipelines.", days_ago=1)
    await _posting(db_session, company, "Analyst", "Kafka pipelines. Streaming.", days_ago=2)
    fake = WordReranker("streaming")
    use(fake)

    found = await retrieve(db_session, "kafka jobs at Hightouch")

    assert found.reranked_by == "fake-reranker"
    assert found.passages[0].title == "Analyst"


# --- the reply says so -------------------------------------------------------------


async def test_the_reply_names_the_model_that_ordered_the_sources(
    client, worker_session, monkeypatch, use
) -> None:
    """Two models now touch an answer's sources; the reply names the second.

    §14 has the reply carry `model` and `local` so an answer never looks like
    one it is not. The order of the sources is part of the answer too.
    """
    import apps.api.routers.chat as chat_module
    from packages.llm.provider import StubProvider

    await _kafka_three(worker_session)
    await worker_session.commit()
    monkeypatch.setattr(chat_module.llm_router, "build_provider", lambda name=None: StubProvider())
    use(WordReranker("streaming"))

    answered = await client.post("/chat", json={"message": "kafka roles"})

    assert answered.status_code == 200
    assert answered.json()["postings_reranked_by"] == "fake-reranker"
    assert [source["title"] for source in answered.json()["sources"]] == ["A", "B", "C"]


async def test_the_reply_says_when_nothing_reordered_them(client, worker_session, monkeypatch, use):
    import apps.api.routers.chat as chat_module
    from packages.llm.provider import StubProvider

    await _kafka_three(worker_session)
    await worker_session.commit()
    monkeypatch.setattr(chat_module.llm_router, "build_provider", lambda name=None: StubProvider())
    use(None)

    answered = await client.post("/chat", json={"message": "kafka roles"})

    assert answered.json()["postings_reranked_by"] is None
