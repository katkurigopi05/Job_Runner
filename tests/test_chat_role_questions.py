"""A question naming a kind of job finds the jobs of that kind, and says how many.

The owner asked the assistant "any jobs with job types AI engineer" on
2026-10-05. It listed three postings titled exactly "AI Engineer", a "Director,
Technical Program Management" role, and said "no skill requirements are
listed". In the owner's search area there were 50 postings with AI and Engineer
in the title and 61 the role table reads as that role. Four separate causes:

- "AI" is in 81% of postings and "engineer" in 39%, so neither was a search
  term. The one word left to search on was **"types"**, which is how the
  director role arrived.
- Only a posting whose *whole* title is in the question was found by title, so
  "Staff AI Engineer" and "Machine Learning Engineer" were not.
- Nothing told the model, or the owner, that there were 58 more.
- Found by title, a posting has no matching term to quote, so its excerpt was
  the first 600 characters: the employer describing itself.

The role table (`matching/roles.py`) already knew the first two; the feed's
role filter uses it. Now the assistant does too.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from packages.core.models import Company, CorpusStats, Posting
from packages.matching import idf
from packages.matching import retrieve as R
from packages.matching.retrieve import excerpt, retrieve

ABOUT = "About us. We are a friendly company building the future of work for everyone. " * 12
REQUIREMENTS = (
    "Requirements\n5+ years of Python.\nExperience shipping PyTorch models to production."
)


@pytest.fixture(autouse=True)
def _no_reranker(monkeypatch):
    monkeypatch.setattr(R, "get_reranker", lambda: None)


async def _company(session, name: str = "Acme") -> Company:
    company = Company(name=name, ats_type="greenhouse")
    session.add(company)
    await session.flush()
    return company


async def _posting(session, company, title, body="A role.", *, days_ago=0, location=None):
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


async def _ai_roles(session):
    """Three postings of one role under three titles, and one that only mentions it."""
    company = await _company(session)
    staff = await _posting(session, company, "Staff AI Engineer", days_ago=1)
    ml = await _posting(session, company, "Machine Learning Engineer", days_ago=2)
    exact = await _posting(session, company, "AI Engineer", days_ago=3)
    manager = await _posting(
        session, company, "Product Manager", "You will work with every AI engineer here."
    )
    return staff, ml, exact, manager


# --- what gets searched for --------------------------------------------------------


@pytest.mark.parametrize("word", ["type", "types", "kind", "kinds"])
def test_asking_for_a_type_of_job_does_not_search_for_the_word_type(word: str) -> None:
    """ "types" was the only term left, and it found a director role."""
    terms = R._search_terms(f"any jobs with job {word} AI engineer", idf.DocumentFrequencies())

    assert word not in terms


# --- the role ------------------------------------------------------------------------


async def test_a_question_naming_a_role_finds_every_title_of_that_role(db_session) -> None:
    staff, ml, exact, manager = await _ai_roles(db_session)

    found = await retrieve(db_session, "any jobs with job types AI engineer")

    ids = [p.posting_id for p in found.passages]
    assert set(ids) == {staff.id, ml.id, exact.id}
    assert manager.id not in ids, "mentioning the role is not being the role"
    assert found.roles == ("machine learning engineer",)
    assert found.role_total == 3


async def test_the_posting_titled_exactly_as_asked_still_goes_first(db_session) -> None:
    _, _, exact, _ = await _ai_roles(db_session)

    found = await retrieve(db_session, "AI engineer jobs")

    assert found.passages[0].posting_id == exact.id


async def test_a_role_and_a_skill_finds_that_role_with_that_skill(db_session) -> None:
    company = await _company(db_session)
    with_skill = await _posting(db_session, company, "AI Engineer", "We run Kafka.", days_ago=2)
    await _posting(db_session, company, "Staff AI Engineer", "We run Postgres.", days_ago=1)
    await _posting(db_session, company, "Chef", "Kafka on toast.")

    found = await retrieve(db_session, "AI engineer jobs using Kafka")

    assert [p.posting_id for p in found.passages] == [with_skill.id]
    assert found.role_total == 2, "how many of the role there are, whatever else was asked"


async def test_a_role_with_a_skill_no_posting_names_falls_back_to_the_role(db_session) -> None:
    """Better the role without the skill than nothing, and the count says which."""
    staff, ml, exact, _ = await _ai_roles(db_session)

    found = await retrieve(db_session, "AI engineer jobs using Fortran")

    assert {p.posting_id for p in found.passages} == {staff.id, ml.id, exact.id}


async def test_a_role_posting_outside_the_search_area_is_left_out_and_counted(db_session) -> None:
    company = await _company(db_session)
    here = await _posting(db_session, company, "AI Engineer", location="San Francisco, CA")
    await _posting(db_session, company, "ML Engineer", location="London, United Kingdom")

    found = await retrieve(db_session, "AI engineer jobs")

    assert [p.posting_id for p in found.passages] == [here.id]
    assert found.role_total == 1
    assert found.outside_area == 1


async def test_a_question_naming_no_role_is_searched_as_before(db_session) -> None:
    company = await _company(db_session)
    kafka = await _posting(db_session, company, "Engineer", "Kafka pipelines.")

    found = await retrieve(db_session, "kafka roles")

    assert [p.posting_id for p in found.passages] == [kafka.id]
    assert found.roles == ()
    assert found.role_total is None


# --- the rest of the list ----------------------------------------------------------


async def test_what_did_not_fit_in_the_answer_is_still_listed(db_session) -> None:
    """Five reach the model. The owner asked for the jobs, not for five of them."""
    company = await _company(db_session)
    postings = [
        await _posting(db_session, company, f"AI Engineer, Team {n}", days_ago=n) for n in range(8)
    ]

    found = await retrieve(db_session, "AI engineer jobs")

    shown = [p.posting_id for p in found.passages]
    listed = [m.posting_id for m in found.more]
    assert len(shown) == 5
    assert len(listed) == 3
    assert set(shown) | set(listed) == {p.id for p in postings}
    assert not set(shown) & set(listed), "nothing is listed twice"
    assert found.role_total == 8


async def test_a_keyword_question_lists_the_rest_of_its_matches_too(db_session) -> None:
    company = await _company(db_session)
    for n in range(7):
        await _posting(db_session, company, f"Engineer {n}", "Kafka pipelines.", days_ago=n)

    found = await retrieve(db_session, "kafka roles")

    assert len(found.passages) == 5
    assert len(found.more) == 2


# --- what the model is shown -------------------------------------------------------


def test_with_nothing_to_quote_the_excerpt_is_the_requirements() -> None:
    """Found by title, a posting has no matching term; its opening is the sales pitch."""
    text = ABOUT + "\n" + REQUIREMENTS

    quoted = excerpt(text, "AI engineer jobs", terms=[])

    assert quoted.startswith("…Requirements")
    assert "PyTorch" in quoted


def test_a_posting_without_a_requirements_heading_is_quoted_from_the_top() -> None:
    text = ABOUT + " We like people who ship."

    assert excerpt(text, "AI engineer jobs", terms=[]).startswith("About us.")


def test_a_matching_term_still_decides_the_excerpt() -> None:
    """The requirements fallback is for when nothing matched, not instead of a match."""
    text = "We use Kafka heavily in the platform team. " + ABOUT + "\n" + REQUIREMENTS

    assert excerpt(text, "kafka roles", terms=["kafka"]).startswith("We use Kafka")


# --- through the API ---------------------------------------------------------------


async def test_the_reply_carries_the_count_and_the_rest_of_the_list(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    import apps.api.routers.chat as chat_module
    from packages.llm.provider import StubProvider

    seen: dict[str, str] = {}

    class Recorder(StubProvider):
        async def complete(self, system: str, user: str, **kwargs) -> str:
            seen["user"] = user
            return "ok [P1]"

    company = await _company(worker_session)
    for n in range(8):
        await _posting(worker_session, company, f"AI Engineer, Team {n}", days_ago=n)
    worker_session.add(CorpusStats(revision=1, total_documents=0, counts_json={}))
    await worker_session.commit()
    monkeypatch.setattr(chat_module.llm_router, "build_provider", lambda name=None: Recorder())

    answered = await client.post("/chat", json={"message": "any jobs with job types AI engineer"})

    body = answered.json()
    assert answered.status_code == 200
    assert body["postings_matched_total"] == 8
    assert body["matched_role"] == "machine learning engineer"
    assert len(body["sources"]) == 5
    assert len(body["more_sources"]) == 3
    assert all(source["cited"] is False for source in body["more_sources"])
    assert "8 open postings" in seen["user"], "the model is told how many there are"
    assert "machine learning engineer" in seen["user"]


# --- the dashboard -----------------------------------------------------------------

DOCK = (
    __import__("pathlib").Path(__file__).resolve().parents[1]
    / "apps/web/src/components/assistant.tsx"
).read_text()


def test_the_dock_keeps_the_rest_of_the_matches_the_reply_sends() -> None:
    """A field the API sends and the dock drops is a list nobody can see."""
    assert "body.more_sources" in DOCK
    assert "body.postings_matched_total" in DOCK
    assert "body.matched_role" in DOCK


def test_the_rest_are_shown_ten_at_a_time() -> None:
    """The owner's words: "stack them up … loading 10 and next 10 and so on"."""
    import re

    assert re.search(r"const MORE_PAGE = 10\b", DOCK), "the page size is not ten"
    assert "setVisible((count) => count + MORE_PAGE)" in DOCK, "nothing loads the next page"
    assert "slice(0, visible)" in DOCK, "the list is not cut to what has been loaded"


def test_a_role_answer_links_to_the_whole_list_on_the_feed() -> None:
    """Past what the reply carries, the feed's role filter is the full list."""
    assert "/matches?role=" in DOCK
