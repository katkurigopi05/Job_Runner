"""A cross-encoder as the assistant's second stage, and the one rule it loosens.

`tests/test_chat_rerank.py` holds a re-ranker that only re-orders. This is the
other kind: a model that reads the question and one passage together and says
whether the passage answers it. Measured on the owner's corpus on 2026-10-08
(49 postings, a natural and a paraphrased question each), re-ordering alone
was not what helped. Three things together took the right posting into the
five shown for 45 natural questions against 37:

- bge-small's nearest postings join the keyword matches as candidates;
- the model reads the title, company and place above the chunk. With the
  chunk alone it made the same questions worse, 24 to 27;
- its order is fused with the search's, not put in place of it.

The first is a change to §14, where keywords decide what is relevant. So it is
held here as narrowly as it was built: only where the keywords found
something, only for a posting the model itself calls an answer, never across
a role's own list, never past the search area or a named company.

No test here loads a model. The judge is a fake that calls a passage an answer
when it says one word.
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
#: What most of a real posting is. Five chunks of it before anything is said.
FILLER = "Our team values ownership and curiosity in everything we ship. " * 40


class WordJudge:
    """Stands in for a cross-encoder: above zero when the passage says the word."""

    name = "fake-judge"
    judges = True

    def __init__(self, word: str) -> None:
        self.word = word
        self.calls: list[tuple[str, list[str]]] = []

    def scores(self, question: str, passages: list[str]) -> list[float]:
        self.calls.append((question, passages))
        return [passage.lower().count(self.word) - 0.5 for passage in passages]


class WordLikeness:
    """The other kind (`test_chat_rerank.py`): a likeness, with no line at zero."""

    name = "fake-likeness"
    judges = False

    def __init__(self, word: str) -> None:
        self.word = word
        self.calls: list[tuple[str, list[str]]] = []

    def scores(self, question: str, passages: list[str]) -> list[float]:
        self.calls.append((question, passages))
        return [float(passage.lower().count(self.word)) for passage in passages]


class BrokenJudge:
    name = "broken-judge"
    judges = True

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


async def _posting(session, company, title, body, *, location=None, days_ago=0) -> Posting:
    posting = Posting(
        company_id=company.id,
        external_id=uuid.uuid4().hex[:8],
        url=f"https://x.test/{uuid.uuid4().hex[:8]}",
        title=title,
        location=location,
        description_raw=body,
        content_hash="h1",
        first_seen_at=datetime.now(UTC) - timedelta(days=days_ago),
    )
    session.add(posting)
    await session.flush()
    return posting


async def _corpus(session, *, elsewhere: str | None = None):
    """One posting the keywords find, and ones only a vector can.

    "streaming" is in most postings, so it is not searched for (§14), and
    "kafka" is what the keyword scan looks for. `said` says Kafka. `unsaid`
    does the same work under another name and never says it, at the end of a
    long posting. `near` shares a word with the question and is about
    something else. `chef` shares nothing.
    """
    session.add(CorpusStats(revision=1, total_documents=100, counts_json={"streaming": 90}))
    acme = await _company(session)
    other = await _company(session, "Globex")
    said = await _posting(session, acme, "Data Engineer", "Kafka pipelines for event streaming.")
    unsaid = await _posting(
        session,
        other,
        "Platform Engineer",
        FILLER + "Event streaming on Redpanda, exactly once.",
        location=elsewhere,
        days_ago=1,
    )
    near = await _posting(
        session, other, "Video Producer", "Live streaming for the events team.", days_ago=2
    )
    chef = await _posting(session, other, "Pastry Chef", "Croissants.", days_ago=3)
    await chunk_postings(session, embedder=LEXICAL)
    return said, unsaid, near, chef


def _shown(found) -> set[uuid.UUID]:
    return {p.posting_id for p in found.passages} | {m.posting_id for m in found.more}


# --- what it adds ------------------------------------------------------------------


async def test_a_posting_worded_differently_can_be_shown(db_session, use) -> None:
    """The gap the keyword rule leaves: the same work under another name."""
    said, unsaid, near, chef = await _corpus(db_session)
    use(WordJudge("redpanda"))

    found = await retrieve(db_session, "kafka streaming roles")

    assert _shown(found) == {said.id, unsaid.id}
    assert found.reranked_by == "fake-judge"


async def test_a_posting_the_model_does_not_call_an_answer_is_not_added(db_session, use) -> None:
    """Near in vector space is not relevant. `near` is nearer the question
    than `chef` and is still about video. Only a posting the model scores as
    an answer joins what the keywords found; what the keywords found stays
    whatever the model thinks of it, as it always has."""
    said, unsaid, near, chef = await _corpus(db_session)
    judge = WordJudge("redpanda")
    use(judge)

    found = await retrieve(db_session, "kafka streaming roles")

    ((_, passages),) = judge.calls
    assert any("Live streaming" in passage for passage in passages), "it was offered and judged"
    assert near.id not in _shown(found)
    assert said.id in _shown(found), "found by keyword, scored below zero, still shown"


async def test_a_word_no_posting_has_still_finds_nothing(db_session, use) -> None:
    """The reason keywords decide (§14, §20): a vector search cannot say
    "none". Asked for a word in no posting it returned five results every
    time. So candidates are added to what the keywords found, never in its
    place: nothing found, nothing added, and the model is not asked."""
    await _corpus(db_session)
    judge = WordJudge("streaming")
    use(judge)

    found = await retrieve(db_session, "zig streaming roles")

    assert found.attempted and found.passages == () and found.more == ()
    assert judge.calls == []


async def test_a_model_that_scores_likeness_adds_nothing(db_session, use, monkeypatch) -> None:
    """An embedding re-ranker's score is a likeness, with no point on it that
    means "this is an answer". It re-orders and that is all."""
    said, unsaid, near, chef = await _corpus(db_session)
    use(WordLikeness("redpanda"))

    async def not_looked_for(*args, **kwargs):
        raise AssertionError("searched for candidates no model will judge")

    monkeypatch.setattr(R, "_nearest_unfound", not_looked_for)

    found = await retrieve(db_session, "kafka streaming roles")

    assert _shown(found) == {said.id}


async def test_a_model_that_scores_likeness_is_not_shown_one_it_is_handed(db_session) -> None:
    """The same rule where the scoring is done, for a caller that forgets it."""
    said, unsaid, near, chef = await _corpus(db_session)
    hits = await R._hits_for(db_session, [said.id, near.id, unsaid.id])
    likeness = WordLikeness("redpanda")

    ordered, _ = await R._reranked(
        likeness, "kafka streaming roles", [hits[said.id], hits[near.id]], {}, [hits[unsaid.id]]
    )

    assert {hit.posting_id for hit in ordered} == {said.id, near.id}
    assert all(len(passages) == 2 for _, passages in likeness.calls)


async def test_adding_can_be_switched_off(db_session, use, monkeypatch) -> None:
    """`CHAT_RERANK_WIDEN=0` is the cross-encoder with §14 as it was."""
    said, unsaid, near, chef = await _corpus(db_session)
    monkeypatch.setattr(R, "_widen_pool", lambda: 0)
    judge = WordJudge("redpanda")
    use(judge)

    found = await retrieve(db_session, "kafka streaming roles")

    assert _shown(found) == {said.id}
    assert not any("Redpanda" in passage for _, passages in judge.calls for passage in passages)


def test_fifty_are_added_unless_the_owner_says_otherwise() -> None:
    assert Settings(_env_file=None).chat_rerank_widen == 50


# --- where it does not reach -------------------------------------------------------


async def test_a_posting_outside_the_search_area_is_not_added(db_session, use) -> None:
    """The owner's area is the feed's rule and the chat's (§14). A candidate
    the keywords did not find is held to it like one they did."""
    said, unsaid, near, chef = await _corpus(db_session, elsewhere="London, United Kingdom")
    judge = WordJudge("redpanda")
    use(judge)

    found = await retrieve(db_session, "kafka streaming roles")

    assert _shown(found) == {said.id}
    assert not any("Redpanda" in passage for _, passages in judge.calls for passage in passages), (
        "a posting that would be left out anyway took a place in the pool"
    )


async def test_a_named_company_is_not_widened_past(db_session, use) -> None:
    said, unsaid, near, chef = await _corpus(db_session)
    use(WordJudge("redpanda"))

    found = await retrieve(db_session, "kafka streaming jobs at Acme")

    assert found.companies == ("Acme",), "the question was read as naming a company"
    assert _shown(found) == {said.id}


async def test_a_roles_own_list_is_not_added_to(db_session, use) -> None:
    """A question that names a role is answered with that role's postings,
    counted and listed by kind of title (§22). A posting of another role does
    not join them for resembling the question."""
    said, unsaid, near, chef = await _corpus(db_session)
    use(WordJudge("redpanda"))

    found = await retrieve(db_session, "data engineer roles using kafka streaming")

    assert found.role_keys, "the question was read as naming a role"
    assert _shown(found) == {said.id}


async def test_a_posting_asked_for_by_title_still_goes_first(db_session, use) -> None:
    """§14: asked for outright, and no model's opinion of its text outranks that."""
    db_session.add(CorpusStats(revision=1, total_documents=100, counts_json={"streaming": 90}))
    company = await _company(db_session)
    exact = await _posting(db_session, company, "Forward Deployed Engineer", "Customer work.")
    other = await _posting(
        db_session, company, "Engineer", "Forward deployed engineer on Redpanda streaming."
    )
    await chunk_postings(db_session, embedder=LEXICAL)
    use(WordJudge("redpanda"))

    found = await retrieve(db_session, "Forward Deployed Engineer")

    assert [p.posting_id for p in found.passages][:2] == [exact.id, other.id]


# --- what it reads -----------------------------------------------------------------


async def test_it_reads_the_title_the_company_and_the_place(db_session, use) -> None:
    """With the chunk alone it made the measured questions worse: a chunk is
    500 characters from the middle of a posting and almost never says what
    the job is called or who is hiring, and a question usually does."""
    company = await _company(db_session)
    await _posting(
        db_session, company, "Data Engineer", "Kafka pipelines.", location="San Francisco, CA"
    )
    await _posting(db_session, company, "Analyst", "Kafka dashboards.", days_ago=1)
    judge = WordJudge("kafka")
    use(judge)

    await retrieve(db_session, "kafka roles")

    ((question, passages),) = judge.calls
    assert question == "kafka roles"
    assert sorted(passages) == [
        "Analyst | Acme\nKafka dashboards.",
        "Data Engineer | Acme | San Francisco, CA\nKafka pipelines.",
    ]


async def test_a_model_that_scores_likeness_still_reads_the_chunk_alone(db_session, use) -> None:
    """The arrangement that was measured for it, left as it was."""
    company = await _company(db_session)
    await _posting(db_session, company, "Data Engineer", "Kafka pipelines.")
    await _posting(db_session, company, "Analyst", "Kafka dashboards.", days_ago=1)
    likeness = WordLikeness("kafka")
    use(likeness)

    await retrieve(db_session, "kafka roles")

    ((_, passages),) = likeness.calls
    assert sorted(passages) == ["Kafka dashboards.", "Kafka pipelines."]


async def test_an_added_posting_is_quoted_where_it_matched(db_session, use) -> None:
    """It has none of the question's keywords, so a keyword excerpt of it
    would quote its opening. The chunk the vector search ranked it by is the
    evidence the model was shown for it."""
    said, unsaid, near, chef = await _corpus(db_session)
    use(WordJudge("redpanda"))

    found = await retrieve(db_session, "kafka streaming roles")

    [added] = [p for p in found.passages if p.posting_id == unsaid.id]
    assert "Redpanda" in added.excerpt


# --- the order ---------------------------------------------------------------------


def test_the_order_is_the_searchs_and_the_models_together() -> None:
    """Put in place of the search's order, a model's order threw away what the
    keyword scan knows (a title match, a place). Fused, the search's first
    choice is not sent to the bottom by one model's opinion of it."""
    # The search says a, b, c, d. The model says b, d, c, a.
    assert rerank.fuse(["a", "b", "c", "d"], [1.0, 4.0, 2.0, 3.0], ranked=4) == [
        "b",
        "a",
        "d",
        "c",
    ]


async def test_the_searchs_first_choice_is_not_sent_last_by_the_model(db_session, use) -> None:
    """Through the search, not only the function. The keyword scan says D, C,
    B, A and the model says C, A, B, D. In place of the search's order that is
    D last. Fused, D is second: it took both to put C above it."""
    company = await _company(db_session)
    a = await _posting(db_session, company, "A", "Kafka. Streaming streaming.", days_ago=3)
    b = await _posting(db_session, company, "B", "Kafka. Streaming.", days_ago=2)
    c = await _posting(
        db_session, company, "C", "Kafka. Streaming streaming streaming.", days_ago=1
    )
    d = await _posting(db_session, company, "D", "Kafka.")
    use(WordJudge("streaming"))

    found = await retrieve(db_session, "kafka roles")

    assert [p.posting_id for p in found.passages] == [c.id, d.id, a.id, b.id]


class ScriptedJudge:
    """Scores a passage by the title on its first line."""

    name = "scripted-judge"
    judges = True

    def __init__(self, by_title: dict[str, float]) -> None:
        self.by_title = by_title

    def scores(self, question: str, passages: list[str]) -> list[float]:
        return [self.by_title[passage.split(" | ")[0].split("\n")[0]] for passage in passages]


def _hit(title: str) -> R._Hit:
    return R._Hit(uuid.uuid4(), title, None, f"https://x.test/{title}", f"{title} work.", "Acme")


async def test_the_models_rank_is_among_everything_it_judged() -> None:
    """A posting it does not call an answer is left out after the ranking,
    not before it. Taken out first, the model's ranks are only among what the
    keywords found and the one posting added, so a keyword match the model
    thinks least of still ranks second of three with it. Here the model puts
    two postings it rejects above both keyword matches. That is what it
    thinks of those matches, and the added posting goes above them.

    It is also the arrangement that was measured: the count of 45 was over
    every candidate, ranked together.
    """
    first, second = _hit("Found first"), _hit("Found second")
    answer, near, nearer = _hit("An answer"), _hit("Near"), _hit("Nearer")
    judge = ScriptedJudge(
        {"An answer": 5.0, "Nearer": -1.0, "Near": -2.0, "Found first": -3.0, "Found second": -4.0}
    )

    ordered, _ = await R._reranked(judge, "a question", [first, second], {}, [answer, near, nearer])

    assert [hit.title for hit in ordered] == ["An answer", "Found first", "Found second"]


def test_ties_keep_the_searchs_order() -> None:
    assert rerank.fuse(["a", "b", "c"], [0.0, 0.0, 0.0], ranked=3) == ["a", "b", "c"]


def test_what_the_search_did_not_rank_takes_the_place_after_its_last() -> None:
    """Two postings were added to a search that found two. Each is ranked by
    the search as if third. The model's favourite of the four goes above the
    search's second choice and not above its first; its least favourite, also
    added, goes last."""
    assert rerank.fuse(["a", "b", "x", "y"], [2.0, 1.0, 9.0, 0.0], ranked=2) == [
        "a",
        "x",
        "b",
        "y",
    ]


def test_fuse_refuses_a_score_list_of_the_wrong_length() -> None:
    with pytest.raises(ValueError, match="2 scores for 3"):
        rerank.fuse(["a", "b", "c"], [1.0, 2.0], ranked=3)


async def test_a_search_answered_by_vectors_alone_loses_nothing(db_session, use) -> None:
    """No distinguishing word, so chunks answer (§14). There the model only
    orders: nothing is added, and nothing found is dropped for scoring low."""
    db_session.add(
        CorpusStats(revision=1, total_documents=100, counts_json={"data": 90, "engineering": 90})
    )
    company = await _company(db_session)
    plain = await _posting(db_session, company, "Engineer", "You will own data engineering.")
    richer = await _posting(
        db_session, company, "Analyst", "Data engineering on Redpanda.", days_ago=1
    )
    await chunk_postings(db_session, embedder=LEXICAL)
    use(WordJudge("redpanda"))

    found = await retrieve(db_session, "any data engineering roles?")

    assert {p.posting_id for p in found.passages} == {plain.id, richer.id}
    assert found.reranked_by == "fake-judge"


# --- when it cannot ----------------------------------------------------------------


async def test_a_judge_that_fails_leaves_the_search_as_it_was(db_session, use) -> None:
    """Nothing re-ordered and nothing added: an unjudged candidate is exactly
    the near-topic posting the keyword rule exists to keep out."""
    said, unsaid, near, chef = await _corpus(db_session)
    use(BrokenJudge())

    found = await retrieve(db_session, "kafka streaming roles")

    assert _shown(found) == {said.id}
    assert found.reranked_by is None


# --- which kind a named model is ---------------------------------------------------


def test_the_model_named_decides_which_kind_it_is(monkeypatch) -> None:
    """One setting. A second saying "this one is a cross-encoder" could
    disagree with the first, and would be read as no preference when it did."""
    built: list[tuple[str, str]] = []

    class Judge:
        judges = True

        def __init__(self, name: str) -> None:
            built.append(("cross-encoder", name))

    class Likeness:
        judges = False

        def __init__(self, name: str) -> None:
            built.append(("embedding", name))

    monkeypatch.setattr(rerank, "CrossEncoderReranker", Judge)
    monkeypatch.setattr(rerank, "EmbeddingReranker", Likeness)
    for name, kind in (("some/cross-encoder", True), ("some/embedder", False)):
        monkeypatch.setenv("CHAT_RERANK_MODEL", name)
        monkeypatch.setattr(rerank, "_is_cross_encoder", lambda _name, kind=kind: kind)
        rerank.get_settings.cache_clear()
        rerank.get_reranker.cache_clear()
        try:
            assert rerank.get_reranker() is not None
        finally:
            rerank.get_reranker.cache_clear()
            rerank.get_settings.cache_clear()

    assert built == [("cross-encoder", "some/cross-encoder"), ("embedding", "some/embedder")]
