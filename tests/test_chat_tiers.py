"""What matches all of a question first, then the rest after it, and nothing dropped.

The owner asked the assistant "any software engineer roles in AI" on
2026-10-20 and got 614 software engineer postings, the first three titled
exactly "Software Engineer" at Roku: the role table read the role, and "AI",
in 81% of descriptions, was dropped as a search term. Asked how it should
work, the owner said: the exact roles first, then the related ones after them,
and do not ignore the rest, because that is where a missed job hides.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from packages.core.models import Company, CorpusStats, Posting
from packages.matching import retrieve as R
from packages.matching import tiers
from packages.matching.retrieve import retrieve


@pytest.fixture(autouse=True)
def _no_reranker(monkeypatch):
    monkeypatch.setattr(R, "get_reranker", lambda: None)


async def _common(session, *words: str) -> None:
    """Corpus statistics in which these words are in 90 of 100 postings."""
    session.add(CorpusStats(revision=1, total_documents=100, counts_json=dict.fromkeys(words, 90)))
    await session.flush()


async def _posting(session, company, title, body="A role.", *, days_ago=0) -> Posting:
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


async def _company(session) -> Company:
    company = Company(name="Acme", ats_type="greenhouse")
    session.add(company)
    await session.flush()
    return company


async def _engineers(session) -> dict[str, Posting]:
    """Software engineers with and without AI in the title, and one not of the role."""
    await _common(session, "ai")
    company = await _company(session)
    return {
        "plain": await _posting(session, company, "Software Engineer", "We use AI.", days_ago=0),
        "ai": await _posting(session, company, "Software Engineer, AI Platform", days_ago=3),
        "mts": await _posting(session, company, "Member of Technical Staff, AI", days_ago=4),
        "payments": await _posting(session, company, "Software Engineer, Payments", days_ago=1),
        "pm": await _posting(session, company, "Product Manager, AI", days_ago=2),
    }


# --- a role, and more asked of it ---------------------------------------------------


async def test_the_role_with_the_word_in_its_title_comes_first(db_session) -> None:
    posts = await _engineers(db_session)

    found = await retrieve(db_session, "any software engineer roles in AI")

    assert {p.posting_id for p in found.passages} == {posts["ai"].id, posts["mts"].id}
    assert posts["plain"].id not in {p.posting_id for p in found.passages}, (
        "titled exactly as the role, and still not ahead of the ones saying AI"
    )
    assert (found.beyond_role, found.beyond_in_title, found.beyond_mentioned) == (("AI",), 2, None)


async def test_the_rest_of_the_role_is_listed_after_and_never_dropped(db_session) -> None:
    posts = await _engineers(db_session)

    found = await retrieve(db_session, "any software engineer roles in AI")

    assert {m.posting_id for m in found.more} == {posts["plain"].id, posts["payments"].id}
    assert {m.kind for m in found.more} == {"software engineer"}
    assert found.role_total == 4
    assert posts["pm"].id not in {m.posting_id for m in found.more}, "not the role"


async def test_each_tier_is_a_heading_ahead_of_the_next(db_session) -> None:
    await _engineers(db_session)

    found = await retrieve(db_session, "any software engineer roles in AI")

    assert [kind for kind, _ in found.kinds] == [
        "software engineer (AI in the title)",
        "member of technical staff (AI in the title)",
        "software engineer",
    ]
    assert sum(count for _, count in found.kinds) == found.role_total


async def test_a_rare_word_in_a_description_is_the_second_tier(db_session) -> None:
    company = await _company(db_session)
    in_title = await _posting(db_session, company, "Software Engineer, Kafka", days_ago=2)
    mentions = await _posting(db_session, company, "Software Engineer", "We run Kafka.")
    rest = await _posting(db_session, company, "Software Engineer II", "We run Postgres.")

    found = await retrieve(db_session, "software engineer jobs using Kafka")

    assert [p.posting_id for p in found.passages] == [in_title.id, mentions.id]
    assert [m.posting_id for m in found.more] == [rest.id]
    assert (found.beyond_in_title, found.beyond_mentioned) == (1, 1)
    assert [kind for kind, _ in found.kinds] == [
        "software engineer (Kafka in the title)",
        "software engineer (mentions Kafka)",
        "software engineer",
    ]


async def test_with_nothing_saying_it_the_role_is_the_answer(db_session) -> None:
    company = await _company(db_session)
    await _posting(db_session, company, "Software Engineer")
    await _posting(db_session, company, "Software Engineer II")

    found = await retrieve(db_session, "software engineer jobs using Fortran")

    assert len(found.passages) == 2
    assert found.beyond_in_title == 0 and found.beyond_mentioned == 0


# --- a word looked for in titles, and a role named with it ----------------------------


async def test_a_role_named_with_the_word_is_listed_after_as_related(db_session) -> None:
    await _common(db_session, "ai")
    company = await _company(db_session)
    titled = await _posting(db_session, company, "Staff AI Engineer")
    ml = await _posting(db_session, company, "Machine Learning Engineer", days_ago=1)

    found = await retrieve(db_session, "any roles for AI")

    assert [p.posting_id for p in found.passages] == [titled.id], "the model gets the AI title"
    assert [(m.posting_id, m.kind) for m in found.more] == [
        (ml.id, "related: machine learning engineer")
    ]
    assert (found.title_total, found.related_total) == (1, 1)


async def test_no_role_named_with_the_word_adds_nothing(db_session) -> None:
    await _common(db_session, "security")
    company = await _company(db_session)
    await _posting(db_session, company, "Security Engineer")
    await _posting(db_session, company, "Software Engineer", days_ago=1)

    found = await retrieve(db_session, "any security roles")

    assert found.related_total == 0
    assert found.title_total == 1


# --- the parts ------------------------------------------------------------------------


def test_every_tier_is_listed_however_long_the_first() -> None:
    """277 titles saying "AI" filled one cap of 100, and the related were never shown."""
    first = [uuid.uuid4() for _ in range(12)]
    related = [uuid.uuid4() for _ in range(3)]
    tier_of = {**dict.fromkeys(first, tiers.IN_TITLE), **dict.fromkeys(related, tiers.REST)}

    shown, listed = tiers.split(first + related, tier_of, limit=5, more=4)

    assert shown == first[:5]
    assert listed == first[5:9] + related[:3]


def test_the_model_is_handed_only_what_matches_when_anything_does() -> None:
    one, others = uuid.uuid4(), [uuid.uuid4() for _ in range(6)]
    tier_of = {one: tiers.MENTIONED, **dict.fromkeys(others, tiers.REST)}

    shown, listed = tiers.split([*others, one], tier_of, limit=5, more=100)

    assert shown == [one]
    assert listed == others


def test_a_role_is_related_when_one_of_its_names_has_the_word() -> None:
    def stem(word: str) -> str:
        return word

    assert tiers.roles_holding(frozenset({"ai"}), stem) == {"machine_learning_engineer"}
    assert tiers.roles_holding(frozenset({"security"}), stem) == set()
    assert tiers.roles_holding(frozenset(), stem) == set()


# --- what the model is told, and the dock -----------------------------------------------


async def test_the_model_is_told_how_many_say_it_and_not_to_count_the_rest(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    import apps.api.routers.chat as chat_module
    from packages.llm.provider import StubProvider

    seen: dict[str, str] = {}

    class Recorder(StubProvider):
        async def complete(self, system: str, user: str, **kwargs) -> str:
            seen["user"] = user
            return "ok"

    await _engineers(worker_session)
    await worker_session.commit()
    monkeypatch.setattr(chat_module.llm_router, "build_provider", lambda name=None: Recorder())

    asked = {"message": "any software engineer roles in AI"}
    body = (await client.post("/chat", json=asked)).json()

    assert len(body["sources"]) == 2
    assert len(body["more_sources"]) == 2
    assert 'Of the 4, 2 have "AI" in the title' in seen["user"]
    assert 'do not count the other 2 as having "AI"' in seen["user"]


async def test_the_reply_carries_the_related_count(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    import apps.api.routers.chat as chat_module
    from packages.llm.provider import StubProvider

    seen: dict[str, str] = {}

    class Recorder(StubProvider):
        async def complete(self, system: str, user: str, **kwargs) -> str:
            seen["user"] = user
            return "ok"

    await _common(worker_session, "ai")
    company = await _company(worker_session)
    await _posting(worker_session, company, "Staff AI Engineer")
    await _posting(worker_session, company, "Machine Learning Engineer", days_ago=1)
    await worker_session.commit()
    monkeypatch.setattr(chat_module.llm_router, "build_provider", lambda name=None: Recorder())

    body = (await client.post("/chat", json={"message": "any roles for AI"})).json()

    assert (body["postings_matched_total"], body["postings_related_total"]) == (1, 1)
    assert "related: 1 more open posting" in seen["user"]
    assert "1 machine learning engineer" in seen["user"]


def test_the_dock_counts_the_related_apart() -> None:
    from pathlib import Path

    dock = (
        Path(__file__).resolve().parents[1] / "apps/web/src/components/assistant.tsx"
    ).read_text()

    assert "body.postings_related_total" in dock
    assert "related without it" in dock
