"""A word too common to search descriptions for is still telling in a title.

The owner asked "any roles with AI" on 2026-10-06 and was shown a data-centre
hardware engineer, an internal-communications role and an account associate,
all at OpenAI. "AI" is in 81% of posting descriptions, so §14's rule dropped
it as a search term; with no term left the search fell back to vectors, and
the nearest things to the two letters "AI" are employers with AI in their
name.

The same word is in 4.5% of *titles*: 222 open postings in the owner's search
area. `data` is 76% against 3.8%, `security` 36% against 3.0%. So when nothing
in a question is rare enough to look for in descriptions, its words are looked
for in titles, and the result is listed the way the owner asked for role
questions: like a dictionary, by kind of title, what was typed first.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from packages.core.models import Company, CorpusStats, Posting
from packages.matching import retrieve as R
from packages.matching.retrieve import retrieve


@pytest.fixture(autouse=True)
def _no_reranker(monkeypatch):
    monkeypatch.setattr(R, "get_reranker", lambda: None)


async def _common(session, *words: str) -> None:
    """Corpus statistics in which these words are in 90 of 100 postings."""
    session.add(CorpusStats(revision=1, total_documents=100, counts_json=dict.fromkeys(words, 90)))
    await session.flush()


async def _company(session, name: str = "Acme") -> Company:
    company = Company(name=name, ats_type="greenhouse")
    session.add(company)
    await session.flush()
    return company


async def _posting(session, company, title, body="A role.", *, days_ago=0):
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


async def _ai_titles(session):
    await _common(session, "ai")
    company = await _company(session)
    return {
        "pm_a": await _posting(session, company, "Product Manager, AI Platform", days_ago=0),
        "swe": await _posting(session, company, "Software Engineer, AI Infrastructure", days_ago=1),
        "pm_b": await _posting(session, company, "Senior Product Manager, AI", days_ago=2),
        "ai": await _posting(session, company, "Staff AI Engineer", days_ago=3),
        "architect": await _posting(session, company, "Applied AI Architect", days_ago=4),
        "chef": await _posting(session, company, "Chef", "We use AI to plan every menu."),
    }


# --- what is found -----------------------------------------------------------------


async def test_a_common_word_is_looked_for_in_titles(db_session) -> None:
    posts = await _ai_titles(db_session)

    found = await retrieve(db_session, "any roles with AI")

    listed = {p.posting_id for p in found.passages} | {m.posting_id for m in found.more}
    assert listed == {posts[k].id for k in ("pm_a", "swe", "pm_b", "ai", "architect")}
    assert posts["chef"].id not in listed, "saying AI in the description is most postings"
    assert found.title_words == ("AI",)
    assert found.title_total == 5


async def test_the_kinds_are_a_dictionary_of_the_titles(db_session) -> None:
    await _ai_titles(db_session)

    found = await retrieve(db_session, "any roles with AI")

    kinds = dict(found.kinds)
    assert kinds["AI engineer"] == 1
    assert kinds["software engineer"] == 1
    assert kinds["product manager"] == 2, "two titles of one unlisted kind are a kind"
    assert kinds["other titles"] == 1, "a one-off title is not a heading of its own"
    assert sum(kinds.values()) == found.title_total


async def test_kinds_that_say_the_word_come_first_and_one_offs_last(db_session) -> None:
    """The owner's order: "AI engineer first then …"."""
    posts = await _ai_titles(db_session)

    found = await retrieve(db_session, "any roles with AI")

    order = [kind for kind, _ in found.kinds]
    assert order[0] == "AI engineer"
    assert order[-1] == "other titles"
    assert found.passages[0].posting_id == posts["ai"].id, "the oldest, and listed first"


async def test_word_forms_match_across_the_question_and_the_title(db_session) -> None:
    """ "data engineering roles" is asking for data engineers."""
    await _common(db_session, "data", "engineering", "engineer")
    company = await _company(db_session)
    engineer = await _posting(db_session, company, "Senior Data Engineer")
    manager = await _posting(db_session, company, "Data Engineering Manager", days_ago=1)
    await _posting(db_session, company, "Chef", "Data engineering for menus.")

    found = await retrieve(db_session, "any data engineering roles?")

    assert {p.posting_id for p in found.passages} == {engineer.id, manager.id}


async def test_within_a_named_company(db_session) -> None:
    await _common(db_session, "ai")
    ours = await _company(db_session, "Hightouch")
    theirs = await _company(db_session, "Acme")
    wanted = await _posting(db_session, ours, "AI Engineer")
    await _posting(db_session, ours, "Recruiter")
    await _posting(db_session, theirs, "AI Engineer")

    found = await retrieve(db_session, "AI roles at Hightouch")

    assert [p.posting_id for p in found.passages] == [wanted.id]


# --- what it leaves alone ------------------------------------------------------------


async def test_a_rare_word_is_still_searched_for_in_descriptions(db_session) -> None:
    """Keywords decide relevance (§14) whenever there is a keyword."""
    await _common(db_session, "ai")
    company = await _company(db_session)
    kafka = await _posting(db_session, company, "Engineer", "We run Kafka.")
    await _posting(db_session, company, "AI Engineer", "We run Postgres.")

    found = await retrieve(db_session, "AI roles using Kafka")

    assert [p.posting_id for p in found.passages] == [kafka.id]
    assert found.title_words == ()
    assert found.title_total is None


async def test_no_title_with_the_word_falls_back_to_the_old_search(db_session) -> None:
    await _common(db_session, "ai")
    company = await _company(db_session)
    await _posting(db_session, company, "Chef", "We use AI to plan every menu.")

    found = await retrieve(db_session, "any roles with AI")

    assert found.title_words == ()
    assert found.kinds == ()


# --- the reply and the dock --------------------------------------------------------


async def test_the_reply_says_how_many_titles_and_of_what_kinds(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    import apps.api.routers.chat as chat_module
    from packages.llm.provider import StubProvider

    seen: dict[str, str] = {}

    class Recorder(StubProvider):
        async def complete(self, system: str, user: str, **kwargs) -> str:
            seen["user"] = user
            return "ok"

    await _ai_titles(worker_session)
    await worker_session.commit()
    monkeypatch.setattr(chat_module.llm_router, "build_provider", lambda name=None: Recorder())

    body = (await client.post("/chat", json={"message": "any roles with AI"})).json()

    assert body["matched_title_words"] == ["AI"]
    assert body["postings_matched_total"] == 5
    assert body["matched_role"] is None, "no role in the table was named"
    assert body["matched_kinds"][0] == {"label": "AI engineer", "count": 1}
    assert len(body["sources"]) == 5
    assert '5 open postings in the search area have "AI" in the title' in seen["user"]
    assert "2 product manager" in seen["user"]


def test_the_dock_reads_the_title_words() -> None:
    from pathlib import Path

    dock = (
        Path(__file__).resolve().parents[1] / "apps/web/src/components/assistant.tsx"
    ).read_text()

    assert "body.matched_title_words" in dock
    assert "in the title" in dock


# --- the headings themselves -------------------------------------------------------


async def test_a_kind_with_every_word_asked_for_outranks_a_bigger_one_with_some(db_session) -> None:
    """Seen on the owner's corpus: "manager 5" listed among the product managers.

    A title's qualifier is dropped to make its kind, so "Engineering Manager,
    Product Platform" is found for "product manager" and filed under
    "engineering manager". It belongs in the list and not at the top of it.
    """
    await _common(db_session, "product", "manager")
    company = await _company(db_session)
    for n in range(2):
        await _posting(db_session, company, f"Product Manager, Growth {n}")
    for n in range(3):
        await _posting(db_session, company, f"Engineering Manager, Product Platform {n}")

    found = await retrieve(db_session, "product manager jobs")

    assert [kind for kind, _ in found.kinds] == ["product manager", "engineering manager"]
    assert found.passages[0].kind == "product manager"


async def test_a_kind_is_not_named_for_the_punctuation_in_a_title(db_session) -> None:
    """ "Sr. Product Marketing Manager" made a kind called ". product marketing manager"."""
    await _common(db_session, "product", "manager")
    company = await _company(db_session)
    await _posting(db_session, company, "Sr. Product Marketing Manager")
    await _posting(db_session, company, "Product Marketing Manager", days_ago=1)

    found = await retrieve(db_session, "product manager jobs")

    assert dict(found.kinds) == {"product marketing manager": 2}


async def test_a_kind_from_the_role_table_comes_before_a_title_family(db_session) -> None:
    """Both say the word; "AI engineer" is a known kind of job, the other is two titles."""
    await _common(db_session, "ai")
    company = await _company(db_session)
    await _posting(db_session, company, "AI Engineer")
    for n in range(3):
        await _posting(db_session, company, f"Applied AI Architect, Region {n}", days_ago=n + 1)

    found = await retrieve(db_session, "any roles with AI")

    assert [kind for kind, _ in found.kinds] == ["AI engineer", "applied AI architect"]
