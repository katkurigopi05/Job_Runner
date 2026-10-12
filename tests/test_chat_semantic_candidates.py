"""Opt-in semantic candidates can recover paraphrases outside the keyword pool.

The re-ranker here scores likeness (`judges = False`), which is the kind this
experiment was written against. A model that judges is handed candidates of its
own by `_nearest_unfound`, and `tests/test_chat_cross_encoder.py` holds that
path — including what each kind of model reads, which is why the title-with-the-
chunk test that used to sit in this file is not here: a likeness model reads the
chunk alone, as it was measured on 2026-10-05.
"""

import pytest

from packages.core.config import Settings
from packages.core.models_chunks import PostingChunk
from packages.matching import retrieve as R
from packages.matching.embed import EMBEDDING_DIM, SentenceTransformerEmbedder
from tests.test_chat_retrieval import _company, _posting


class SemanticEncoder:
    def encode(self, texts):
        return [[1.0] + [0.0] * (EMBEDDING_DIM - 1) for _ in texts]


class RecordingReranker:
    name = "test-semantic-reranker"
    #: Likeness, not judgement: it only re-orders what it is handed.
    judges = False

    def __init__(self):
        self.passages = []

    def scores(self, question, passages):
        self.passages = passages
        return [float("retrieval-augmented generation" in p) for p in passages]


@pytest.fixture
def experiment(monkeypatch):
    settings = Settings(_env_file=None, chat_semantic_candidates=True)
    monkeypatch.setattr(R, "get_settings", lambda: settings)
    monkeypatch.setattr(R, "get_reranker", lambda: None)
    monkeypatch.setattr(
        R,
        "_encoders",
        lambda spaces, *_: {
            space: SemanticEncoder()
            for space in spaces
            if space[0] == SentenceTransformerEmbedder.name
        },
    )
    return settings


async def _chunk(session, posting, *, stale=False):
    session.add(
        PostingChunk(
            posting_id=posting.id,
            ordinal=0,
            start=0,
            length=len(posting.description_raw),
            content_hash="stale" if stale else posting.content_hash,
            embedding_model=SentenceTransformerEmbedder.name,
            embedding=[1.0] + [0.0] * (EMBEDDING_DIM - 1),
        )
    )
    await session.flush()


def _ids(found):
    return [p.posting_id for p in (*found.passages, *found.more)]


async def test_semantic_candidate_reaches_reranker_without_keyword_overlap(
    db_session, experiment, monkeypatch
):
    company = await _company(db_session)
    literal = await _posting(db_session, company, "Records Clerk", "File documents.")
    semantic = await _posting(
        db_session, company, "Engineer", "Build retrieval-augmented generation systems."
    )
    await _chunk(db_session, semantic)
    reranker = RecordingReranker()
    monkeypatch.setattr(R, "get_reranker", lambda: reranker)

    found = await R.retrieve(db_session, "jobs grounding answers in documents")

    assert found.passages[0].posting_id == semantic.id
    assert set(_ids(found)) == {literal.id, semantic.id}
    assert "retrieval-augmented generation" in found.passages[0].excerpt
    assert len(reranker.passages) == 2
    assert found.reranked_by == reranker.name


async def test_flag_off_keeps_keyword_candidates(db_session, experiment):
    experiment.chat_semantic_candidates = False
    company = await _company(db_session)
    literal = await _posting(db_session, company, "Records Clerk", "File documents.")
    semantic = await _posting(db_session, company, "Engineer", "Build retrieval systems.")
    await _chunk(db_session, semantic)

    assert _ids(await R.retrieve(db_session, "jobs using documents")) == [literal.id]


async def test_semantic_only_results_can_fill_an_empty_keyword_pool(db_session, experiment):
    company = await _company(db_session)
    semantic = await _posting(db_session, company, "Engineer", "Build retrieval systems.")
    await _chunk(db_session, semantic)

    assert _ids(await R.retrieve(db_session, "jobs grounding answers")) == [semantic.id]


async def test_semantic_candidates_keep_company_area_closed_and_freshness_filters(
    db_session, experiment
):
    company = await _company(db_session, "Streamco")
    other = await _company(db_session, "Otherco")
    kept = await _posting(db_session, company, "Engineer", "Retrieval systems.")
    for posting in (
        kept,
        await _posting(db_session, other, "Engineer", "Retrieval systems."),
        await _posting(
            db_session, company, "Engineer", "Retrieval systems.", location="London, UK"
        ),
        await _posting(db_session, company, "Engineer", "Retrieval systems.", closed=True),
    ):
        await _chunk(db_session, posting)
    stale = await _posting(db_session, company, "Engineer", "Retrieval systems.")
    await _chunk(db_session, stale, stale=True)

    found = await R.retrieve(db_session, "jobs grounding answers at Streamco")

    assert _ids(found) == [kept.id]


async def test_explicit_role_does_not_expand_into_other_roles(db_session, experiment):
    company = await _company(db_session)
    engineer = await _posting(db_session, company, "AI Engineer", "Kafka services.")
    chef = await _posting(db_session, company, "Chef", "Cook pastries.")
    await _chunk(db_session, chef)

    assert _ids(await R.retrieve(db_session, "AI engineer jobs using Kafka")) == [engineer.id]


async def test_a_candidate_found_both_ways_is_listed_once(db_session, experiment):
    company = await _company(db_session)
    posting = await _posting(db_session, company, "Engineer", "Grounded answers.")
    await _chunk(db_session, posting)

    assert _ids(await R.retrieve(db_session, "jobs with grounded answers")) == [posting.id]


async def test_no_semantic_encoder_preserves_keyword_results(db_session, experiment, monkeypatch):
    company = await _company(db_session)
    posting = await _posting(db_session, company, "Engineer", "Kafka systems.")
    await _chunk(db_session, posting)
    monkeypatch.setattr(R, "_encoders", lambda *_: {})

    assert _ids(await R.retrieve(db_session, "Kafka jobs")) == [posting.id]


async def test_application_status_does_not_start_semantic_search(db_session, experiment):
    company = await _company(db_session)
    posting = await _posting(db_session, company, "Engineer", "Review documents.")
    await _chunk(db_session, posting)

    found = await R.retrieve(db_session, "what needs me?")

    assert not found.attempted
    assert not found.passages


async def test_semantic_expansion_is_bounded(db_session, experiment):
    company = await _company(db_session)
    for _ in range(R.KEYWORD_POOL + 5):
        posting = await _posting(db_session, company, "Engineer", "Retrieval systems.")
        await _chunk(db_session, posting)

    found = await R.retrieve(db_session, "jobs grounding answers")

    assert len(_ids(found)) == R.KEYWORD_POOL


async def test_an_exact_title_stays_first_with_semantic_candidates(
    db_session, experiment, monkeypatch
):
    company = await _company(db_session)
    named = await _posting(db_session, company, "Knowledge Architect", "Design systems.")
    semantic = await _posting(
        db_session, company, "Engineer", "Build retrieval-augmented generation systems."
    )
    await _chunk(db_session, semantic)
    monkeypatch.setattr(R, "get_reranker", lambda: RecordingReranker())

    found = await R.retrieve(db_session, "Knowledge Architect jobs")

    assert found.passages[0].posting_id == named.id
    assert semantic.id in _ids(found)


def test_semantic_expansion_is_off_by_default():
    assert Settings(_env_file=None).chat_semantic_candidates is False


# --------------------------------------------------------------------------
# What the question's own words name keeps its place
# --------------------------------------------------------------------------
#
# Measured on 2026-10-10 over 52 natural questions, each written from one
# posting: adding semantic candidates took the posting out of the five shown
# for three of them, and the re-ranker for two more ("What Primary Care Nurse
# Practitioner jobs are available for adult patient care?", third to
# twentieth; "What Support Engineer I weekend shift roles are open?", second
# to twelfth). In each the question had said the title's words. The second
# list was merged as though it knew as much as the first, and the second
# model read a 500-character chunk that does not contain the title.


class PrefersTheOther:
    """A second model that likes whatever says "incident" and nothing else."""

    name = "test-prefers-the-other"
    #: Likeness, as above.
    judges = False

    def __init__(self):
        self.passages: list[str] = []

    def scores(self, question, passages):
        self.passages = list(passages)
        return [float("incident" in passage) for passage in passages]


QUESTION = "What Support Engineer weekend shift roles are open?"


async def _named_and_liked(session):
    """One posting the question names by its title's words, and one a model prefers."""
    company = await _company(session)
    named = await _posting(
        session, company, "Support Engineer I (FC, Weekend Shift)", "Answer tickets from customers."
    )
    liked = await _posting(
        session, company, "Reliability Lead", "Weekend incident support for engineers on call."
    )
    await _chunk(session, liked)
    return named, liked


async def test_semantic_candidates_do_not_go_ahead_of_a_title_the_question_names(
    db_session, experiment
):
    named, liked = await _named_and_liked(db_session)

    found = await R.retrieve(db_session, QUESTION)

    assert _ids(found)[0] == named.id
    assert liked.id in _ids(found)


async def test_the_second_model_does_not_demote_a_title_the_question_names(
    db_session, experiment, monkeypatch
):
    named, liked = await _named_and_liked(db_session)
    reranker = PrefersTheOther()
    monkeypatch.setattr(R, "get_reranker", lambda: reranker)

    found = await R.retrieve(db_session, QUESTION)

    assert _ids(found)[0] == named.id, "the question said this title's words"
    assert not any("Support Engineer I" in passage for passage in reranker.passages), (
        "it keeps its place, so it takes none of the second model's thirty"
    )


async def test_the_same_holds_with_the_second_model_and_no_semantic_candidates(
    db_session, experiment, monkeypatch
):
    """The re-ranker alone did this too, before any candidate was added (§21)."""
    experiment.chat_semantic_candidates = False
    named, liked = await _named_and_liked(db_session)
    monkeypatch.setattr(R, "get_reranker", lambda: PrefersTheOther())

    found = await R.retrieve(db_session, QUESTION)

    assert _ids(found) == [named.id, liked.id]


async def test_one_word_shared_with_a_title_does_not_hold_a_place(
    db_session, experiment, monkeypatch
):
    """A paraphrase shares a word with many titles by chance. Two is what was
    measured to tell a named title from a coincidence."""
    company = await _company(db_session)
    chance = await _posting(db_session, company, "Customer Support Lead", "Lead the team.")
    liked = await _posting(
        db_session, company, "Reliability Lead", "Handle each incident for people using it."
    )
    await _chunk(db_session, liked)
    monkeypatch.setattr(R, "get_reranker", lambda: PrefersTheOther())

    found = await R.retrieve(db_session, "jobs giving support to people when things break")

    assert _ids(found) == [liked.id, chance.id]


async def test_with_neither_on_the_order_is_the_keyword_order(db_session, experiment):
    """Nothing is held ahead when nothing could have displaced it."""
    experiment.chat_semantic_candidates = False
    company = await _company(db_session)
    first = await _posting(db_session, company, "Reliability Lead", "Weekend shift support.")
    second = await _posting(
        db_session, company, "Support Engineer I (FC, Weekend Shift)", "Answer tickets."
    )

    found = await R.retrieve(db_session, "weekend shift support roles")
    held = R._named_by_its_title(
        R._Hit(second.id, second.title, None, second.url, None, None),
        ["weekend", "shift", "support"],
    )

    assert held, "the title holds all three words"
    assert set(_ids(found)) == {first.id, second.id}
    # The keyword scan's own order: the title match counts twice there already.
    assert _ids(found)[0] == second.id


def test_a_question_with_one_search_term_names_no_title() -> None:
    """ "Customer Experience Manager roles at a global network security
    company" has one word rare enough to search for, and it is "network".
    Letting the only term be enough held thirty Network Engineer titles ahead
    of the job asked for."""
    network_engineer = R._Hit(None, "Senior Network Engineer", None, "u", None, None)

    assert not R._named_by_its_title(network_engineer, ["network"])
    assert not R._named_by_its_title(network_engineer, [])
    assert R._named_by_its_title(network_engineer, ["network", "senior", "kafka"])
