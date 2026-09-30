"""Retrieval for the assistant — the R in front of the chat model.

Most of what can go wrong here does not raise. A question compared against a
vector from another model returns a plausible distance; a posting with no
vector is simply absent; an excerpt cut from the top of a description quotes
the employer's mission statement; a question about the owner's own
applications gets five unrelated postings stapled to it. Each test below pins
one of those, because every one of them leaves the assistant answering
fluently from the wrong material.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import (
    Application,
    Candidate,
    Company,
    CorpusStats,
    Posting,
    Profile,
    User,
)
from packages.llm.provider import StubProvider
from packages.matching.embed import LexicalEmbedder
from packages.matching.retrieve import EXCERPT_CHARS, excerpt, retrieve

LEXICAL = LexicalEmbedder()

KAFKA = (
    "Senior Data Engineer",
    "About us. We are a payments company with a mission to grow the internet economy. "
    "Our culture values ownership and curiosity. "
    "In this role you will build streaming pipelines on Kafka and Flink, and own the "
    "schema registry that every producer depends on.",
)
FRONTEND = (
    "Frontend Engineer",
    "Build our design system in React and TypeScript. Own accessibility and "
    "performance budgets across the dashboard.",
)
CHEF = ("Pastry Chef", "Laminated doughs, viennoiserie, early mornings in a busy kitchen.")


async def _company(session: AsyncSession, name: str = "Acme") -> Company:
    company = Company(name=name, ats_type="greenhouse")
    session.add(company)
    await session.flush()
    return company


async def _posting(
    session: AsyncSession,
    company: Company,
    title: str,
    body: str,
    *,
    stamp: str = LEXICAL.name,
    revision: int | None = None,
    embedded: bool = True,
    vector: list[float] | None = None,
    closed: bool = False,
    location: str = "Remote, US",
) -> Posting:
    posting = Posting(
        company_id=company.id,
        url=f"https://boards.greenhouse.io/acme/jobs/{uuid.uuid4().hex[:8]}",
        title=title,
        location=location,
        description_raw=body,
        description_embedding=(vector or LEXICAL.encode_one(f"{title}\n{body}"))
        if embedded
        else None,
        embedding_model=stamp if embedded else None,
        embedding_revision=revision,
        closed_at=datetime.now(UTC) if closed else None,
    )
    session.add(posting)
    await session.flush()
    return posting


async def _common(session: AsyncSession, *words: str) -> None:
    """Corpus statistics in which `words` are in 90% of postings.

    Enough to make them non-distinguishing, which leaves a question built from
    them with nothing to search for by keyword — the only case where vectors
    answer on their own.
    """
    session.add(CorpusStats(revision=1, total_documents=100, counts_json=dict.fromkeys(words, 90)))
    await session.flush()


# --- when to search -----------------------------------------------------------


async def test_a_question_about_applications_searches_nothing(db_session) -> None:
    """Measured on the owner's database: "what needs me?" came back with five
    customer-care roles, because a nearest-neighbour search always returns its
    nearest few. The question was never about postings."""
    company = await _company(db_session)
    await _posting(db_session, company, *KAFKA)

    for question in ("what needs me?", "Have I had any replies?", "How's it going?"):
        found = await retrieve(db_session, question)
        assert not found.attempted, question
        assert found.passages == ()


async def test_naming_a_company_is_a_question_about_postings(db_session) -> None:
    """ "Anything at Streamco?" names no job word and is still about postings."""
    streamco = await _company(db_session, "Streamco")
    other = await _company(db_session, "Otherco")
    ours = await _posting(db_session, streamco, *KAFKA)
    await _posting(db_session, other, *KAFKA)

    found = await retrieve(db_session, "anything with Kafka at Streamco?")

    assert found.attempted
    assert [p.posting_id for p in found.passages] == [ours.id], "only the named company's"


async def test_a_question_ending_in_a_full_stop_is_still_recognised(db_session) -> None:
    """ "Show me remote jobs." ends in the word "jobs.", not "jobs"."""
    streamco = await _company(db_session, "Streamco")
    await _posting(db_session, streamco, *KAFKA)

    assert (await retrieve(db_session, "Show me Kafka jobs.")).attempted
    assert (await retrieve(db_session, "Anything with Kafka at Streamco.")).attempted


async def test_a_named_company_filters_rather_than_matches(db_session) -> None:
    """ "Remote roles at Streamco": the name is in every Streamco posting.

    Searched for as a term, it padded the answer with Streamco postings that
    never say remote.
    """
    streamco = await _company(db_session, "Streamco")
    remote = await _posting(db_session, streamco, *KAFKA, location="Remote, US")
    await _posting(db_session, streamco, *FRONTEND, location="Berlin")

    found = await retrieve(db_session, "remote roles at Streamco")

    assert [p.posting_id for p in found.passages] == [remote.id]


async def test_naming_only_a_company_lists_its_postings(db_session) -> None:
    """With nothing else asked, the name is the search, unembedded postings included."""
    streamco = await _company(db_session, "Streamco")
    embedded = await _posting(db_session, streamco, *KAFKA)
    unembedded = await _posting(db_session, streamco, *FRONTEND, embedded=False)

    found = await retrieve(db_session, "jobs at Streamco")

    assert {p.posting_id for p in found.passages} == {embedded.id, unembedded.id}


# --- what is relevant ---------------------------------------------------------


async def test_the_posting_that_answers_the_question_ranks_first(db_session) -> None:
    company = await _company(db_session)
    for title, body in (FRONTEND, KAFKA, CHEF):
        await _posting(db_session, company, title, body)

    found = await retrieve(db_session, "which roles work with Kafka streaming?")

    assert found.passages, "nothing was retrieved for a question the corpus answers"
    assert found.passages[0].title == "Senior Data Engineer"
    assert found.passages[0].label == "P1"
    assert found.passages[0].company == "Acme"


async def test_framing_words_are_not_searched_for(db_session) -> None:
    """ "Which open roles…" is looking for what comes after it.

    On the owner's corpus "open" appears in 28% of postings and "roles" in
    35%. Searched for, they matched most of the feed.
    """
    company = await _company(db_session)
    await _posting(db_session, company, "Barista", "Open roles on the morning shift, which rotate.")

    found = await retrieve(db_session, "which open roles use Kafka?")

    assert found.attempted
    assert found.passages == ()


async def test_the_asker_is_not_searched_for(db_session) -> None:
    """ "Show me Kafka jobs." is looking for Kafka.

    Measured on the owner's database: "me" is in 1.9% of postings, so rarity
    kept it, and a request phrased this way returned Pleo postings that say
    "Show me the benefits!".
    """
    company = await _company(db_session)
    await _posting(db_session, company, "AML Analyst", "Show me the benefits! Lunch is on us.")
    kafka = await _posting(db_session, company, *KAFKA)

    found = await retrieve(db_session, "Show me Kafka jobs.")

    assert [p.posting_id for p in found.passages] == [kafka.id]


async def test_a_term_matches_a_whole_word_only(db_session) -> None:
    """ "rust" is not in "trust", and "go" is not in "good"."""
    company = await _company(db_session)
    await _posting(db_session, company, "Account Manager", "Build trust with good clients.")
    rusty = await _posting(db_session, company, "Systems Engineer", "Write Rust services.")

    found = await retrieve(db_session, "roles using Rust or Go")

    assert [p.posting_id for p in found.passages] == [rusty.id]


async def test_a_term_with_punctuation_is_matched_literally(db_session) -> None:
    """ "c++" and "node.js" carry regex metacharacters."""
    company = await _company(db_session)
    cpp = await _posting(db_session, company, "Engine Programmer", "Modern C++ on consoles.")
    await _posting(db_session, company, "Writer", "Grade C prose, nodeXjs.")

    found = await retrieve(db_session, "jobs using c++")

    assert [p.posting_id for p in found.passages] == [cpp.id]


async def test_a_pasted_paragraph_searches_a_bounded_number_of_terms(db_session) -> None:
    """Each term is a regex over every open posting; a message may be 4,000 chars."""
    from packages.matching import idf
    from packages.matching.retrieve import MAX_TERMS, _search_terms

    pasted = " ".join(f"term{i}" for i in range(200)) + " roles"
    assert len(_search_terms(pasted, idf.DocumentFrequencies())) == MAX_TERMS

    company = await _company(db_session)
    await _posting(db_session, company, *KAFKA)
    found = await retrieve(db_session, pasted)
    assert found.attempted


async def test_the_keyword_scan_reaches_postings_with_no_vector(db_session) -> None:
    """A third of the owner's open postings had not been embedded yet.

    A vector search cannot see them at all. The keyword scan can, and a
    closed posting is never a result either way.
    """
    company = await _company(db_session)
    await _posting(db_session, company, *KAFKA, closed=True)
    unembedded = await _posting(db_session, company, *KAFKA, embedded=False)
    embedded = await _posting(db_session, company, *KAFKA)

    found = await retrieve(db_session, "Kafka roles")

    assert {p.posting_id for p in found.passages} == {unembedded.id, embedded.id}
    assert (found.searched, found.unsearchable) == (2, 0)


async def test_a_posting_that_shares_nothing_with_the_question_is_not_returned(
    db_session,
) -> None:
    company = await _company(db_session)
    await _posting(db_session, company, *CHEF)

    found = await retrieve(db_session, "which roles work with Kafka streaming?")

    assert found.passages == ()
    assert found.searched == 1


async def test_terms_that_match_nothing_are_not_replaced_by_nearest_vectors(
    db_session,
) -> None:
    """ "Roles using Zig?" with no posting that says Zig.

    The stored vector below is as close to the question as a vector can be,
    and the text never mentions it. bge-small's similarity is almost never
    zero, so a vector fallback here would hand over its nearest five whatever
    they said — the HelloFresh failure, back through a side door.
    """
    company = await _company(db_session)
    await _posting(
        db_session,
        company,
        "Office Manager",
        "Keep the office running.",
        vector=LEXICAL.encode_one("zig"),
    )

    found = await retrieve(db_session, "roles using Zig?")

    assert found.attempted
    assert found.passages == ()


async def test_a_posting_with_no_vector_keeps_its_keyword_rank(db_session) -> None:
    """Fusion gives a missing vector the middle rank, not no rank.

    The unembedded posting matches both terms and the embedded one only one.
    Scoring the first on one fused term and the second on two put the weaker
    match on top because of what the matching pass had not got to yet.
    """
    company = await _company(db_session)
    both = await _posting(
        db_session, company, "Data Engineer", "Kafka and Flink pipelines.", embedded=False
    )
    one = await _posting(db_session, company, "Data Engineer", "Kafka pipelines.")

    found = await retrieve(db_session, "roles with kafka and flink")

    assert [p.posting_id for p in found.passages] == [both.id, one.id]


async def test_a_question_with_nothing_distinctive_is_answered_by_vectors(db_session) -> None:
    """ "Any data engineering roles?" — both words are in most postings.

    With nothing to search for by keyword, the vectors answer, still above a
    similarity of zero.
    """
    await _common(db_session, "data", "engineering")
    company = await _company(db_session)
    data = await _posting(db_session, company, *KAFKA)
    await _posting(db_session, company, *CHEF)

    found = await retrieve(db_session, "any data engineering roles?")

    assert [p.posting_id for p in found.passages] == [data.id]


# --- which vectors may be compared --------------------------------------------


async def test_a_vector_from_a_model_that_cannot_be_rebuilt_is_never_compared(
    db_session,
) -> None:
    """Cross-space cosine does not fail, it returns a plausible number.

    The stored vector below matches the question exactly, and nothing in the
    text does, so only a vector comparison could return it — against a model
    this process cannot encode a question with.
    """
    await _common(db_session, "data", "engineering")
    company = await _company(db_session)
    question = "data engineering roles"
    await _posting(
        db_session,
        company,
        "Office Manager",
        "Keep the office running.",
        stamp="some-retired-model",
        vector=LEXICAL.encode_one(question),
    )

    found = await retrieve(db_session, question)

    assert found.passages == ()
    assert (found.searched, found.unsearchable) == (0, 1)


async def test_weighted_vectors_from_stale_statistics_are_not_compared(db_session) -> None:
    """`lexical-idf` vectors are only comparable under the weights that built them.

    The active statistics are revision 1; these vectors claim revision 7.
    """
    await _common(db_session, "data", "engineering")
    company = await _company(db_session)
    question = "data engineering roles"
    await _posting(
        db_session,
        company,
        "Office Manager",
        "Keep the office running.",
        stamp="lexical-idf@2",
        revision=7,
        vector=LEXICAL.encode_one(question),
    )

    found = await retrieve(db_session, question)

    assert found.passages == ()
    assert found.unsearchable == 1


async def test_a_question_with_no_searchable_words_searches_nothing(db_session) -> None:
    """ "job?" asks about postings and encodes to a zero vector.

    A zero vector has no direction, and pgvector returns NaN for its distance
    rather than "far".
    """
    company = await _company(db_session)
    await _posting(db_session, company, *KAFKA)

    found = await retrieve(db_session, "job?")

    assert found.attempted
    assert found.passages == ()


async def test_a_posting_already_applied_to_says_so(db_session) -> None:
    company = await _company(db_session)
    posting = await _posting(db_session, company, *KAFKA)
    user = User(email=f"o-{uuid.uuid4().hex[:8]}@example.com")
    db_session.add(user)
    await db_session.flush()
    candidate = Candidate(user_id=user.id, name="Owner", email="c@example.com")
    db_session.add(candidate)
    await db_session.flush()
    profile = Profile(candidate_id=candidate.id, label="p")
    db_session.add(profile)
    await db_session.flush()
    db_session.add(
        Application(
            candidate_id=candidate.id,
            profile_id=profile.id,
            posting_id=posting.id,
            url=posting.url,
            status="needs_review",
        )
    )
    await db_session.flush()

    found = await retrieve(db_session, "which roles use Kafka?")

    assert found.passages[0].application_status == "needs_review"


# --- excerpt ------------------------------------------------------------------


def test_the_excerpt_is_the_part_that_matches_not_the_mission_statement() -> None:
    """Real descriptions open with the employer describing itself.

    Sampled from the owner's database: "Who we are About Stripe Stripe is a
    financial infrastructure platform…" opens every Stripe posting. Quoting
    the top would hand the model the same paragraph for every result.
    """
    body = ("We are a company that believes in building great things together. " * 20) + (
        "You will operate Kafka clusters and tune consumer lag across regions."
    )

    chosen = excerpt(body, "Kafka consumer lag")

    assert chosen.startswith("…You will operate Kafka"), "opens on the matching sentence"
    assert len(chosen) <= EXCERPT_CHARS + 2, "the ellipses are the only overrun allowed"


def test_a_named_company_does_not_choose_the_excerpt() -> None:
    """Every Stripe posting opens by naming Stripe, so the name selects nothing."""
    body = ("Who we are. About Stripe, a payments company for businesses. " * 12) + (
        "This role is open to remote candidates in the US."
    )

    chosen = excerpt(body, "remote roles at Stripe", ignore={"stripe"})

    assert "remote candidates" in chosen


def test_a_short_description_is_quoted_whole() -> None:
    assert excerpt("Build pipelines on Kafka.", "Kafka") == "Build pipelines on Kafka."


def test_a_question_with_no_terms_quotes_the_start() -> None:
    body = "First sentence here. " * 60
    chosen = excerpt(body, "is it?")
    assert chosen.startswith("First sentence here.")
    assert chosen.endswith("…")


# --- through the chat route ---------------------------------------------------


def _recorder(reply: str) -> tuple[type[StubProvider], dict[str, str]]:
    seen: dict[str, str] = {}

    class Recorder(StubProvider):
        async def complete(
            self,
            system: str,
            user: str,
            *,
            max_tokens: int = 1024,
            temperature: float = 0.2,
        ) -> str:
            seen["system"] = system
            seen["user"] = user
            return reply

    return Recorder, seen


async def test_the_model_is_handed_the_retrieved_postings(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    import apps.api.routers.chat as chat_module

    company = await _company(worker_session, "Streamco")
    for title, body in (KAFKA, FRONTEND):
        await _posting(worker_session, company, title, body)
    await worker_session.commit()

    recorder, seen = _recorder("Streamco's data role uses Kafka [P1].")
    monkeypatch.setattr(chat_module.llm_router, "build_provider", lambda name=None: recorder())

    answered = await client.post("/chat", json={"message": "any Kafka roles open?"})

    assert answered.status_code == 200
    assert "[P1] Senior Data Engineer — Streamco" in seen["user"]
    assert "Kafka and Flink" in seen["user"], "the excerpt is what the model can quote"
    assert "cite" in seen["system"].lower()

    body = answered.json()
    assert body["postings_searched"] == 2
    cited = [source for source in body["sources"] if source["cited"]]
    assert [source["title"] for source in cited] == ["Senior Data Engineer"]


async def test_a_question_that_is_not_about_postings_says_they_were_not_searched(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    """Said, not omitted — an absent section reads as "nothing matched"."""
    import apps.api.routers.chat as chat_module

    company = await _company(worker_session)
    await _posting(worker_session, company, *KAFKA)
    await worker_session.commit()

    recorder, seen = _recorder("ok")
    monkeypatch.setattr(chat_module.llm_router, "build_provider", lambda name=None: recorder())

    answered = await client.post("/chat", json={"message": "what needs me?"})

    assert answered.status_code == 200
    assert "POSTINGS: not searched" in seen["user"]
    assert answered.json()["sources"] == []


async def test_the_search_coverage_is_in_the_context(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    """ "No matches" means less when half the corpus could not be searched.

    Nothing in this question is distinctive, so only vectors can answer, and
    one posting has none. The model is told, so it can say "none of the
    postings I could search" rather than "none exist".
    """
    import apps.api.routers.chat as chat_module

    await _common(worker_session, "data", "engineering")
    company = await _company(worker_session)
    await _posting(worker_session, company, *KAFKA)
    await _posting(worker_session, company, *FRONTEND, embedded=False)
    await worker_session.commit()

    recorder, seen = _recorder("ok")
    monkeypatch.setattr(chat_module.llm_router, "build_provider", lambda name=None: recorder())

    answered = await client.post("/chat", json={"message": "any data engineering roles?"})

    assert answered.status_code == 200
    assert "searched 1 of 2 open postings" in seen["user"]
    assert answered.json()["postings_unsearchable"] == 1


async def test_a_refused_question_retrieves_nothing(client: AsyncClient) -> None:
    """§2.2 stops the question before any of the context is built."""
    answered = await client.post("/chat", json={"message": "what salary should I ask for?"})

    assert answered.status_code == 200
    assert answered.json()["provider"] == "refused"
    assert answered.json()["sources"] == []


def test_citations_are_read_in_every_shape_a_model_writes_them() -> None:
    from apps.api.routers.chat import cited_labels

    assert cited_labels("Kafka [P1], and [P2][P4]; also [P3, P5].") == {
        "P1",
        "P2",
        "P3",
        "P4",
        "P5",
    }
    assert cited_labels("Band P1 pays more.") == set(), "only bracketed labels count"
