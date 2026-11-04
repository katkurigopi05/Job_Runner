"""'How many jobs are available?' is answered with the counts, in code.

The question names nothing to search for, so it was not searched, and the
model said it could not give a total (seen 2026-11-02 on Kaggle's Qwen3.8
27B). The totals are the percentage answer's denominator, already counted
in code under the same area rule.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest

from packages.core.models import Company, Posting
from packages.llm.provider import StubProvider
from packages.matching import retrieve as R

ASKED = "how many job posting are available"


@pytest.fixture(autouse=True)
def no_models(monkeypatch):
    monkeypatch.setattr(R, "get_reranker", lambda: None)
    monkeypatch.setattr(R, "_encoders", lambda *args: {})


async def posting(session, title, location="San Francisco, CA", *, company=None, closed=False):
    if company is None:
        company = Company(name="ExampleCo " + uuid.uuid4().hex[:8], ats_type="greenhouse")
        session.add(company)
        await session.flush()
    session.add(
        Posting(
            company_id=company.id,
            title=title,
            location=location,
            url="https://jobs.example.test/" + uuid.uuid4().hex,
            description_raw="A role.",
            content_hash=uuid.uuid4().hex,
            closed_at=datetime.now(UTC) if closed else None,
        )
    )
    await session.flush()
    return company


async def corpus(session) -> None:
    """Five open postings at four companies, three in the search area; one closed."""
    acme = await posting(session, "Data Engineer")
    await posting(session, "Designer", "Oakland, CA", company=acme)
    await posting(session, "ML Engineer", "London, United Kingdom")
    await posting(session, "Data Analyst", "Boston, MA")
    await posting(session, "Product Manager", "Remote - US")
    await posting(session, "AI Engineer", closed=True)


@pytest.mark.parametrize(
    "question",
    [
        ASKED,
        "How many jobs are there?",
        "how many open postings do we have",
        "How many roles are open right now?",
    ],
)
async def test_a_count_of_postings_that_names_nothing_gets_the_totals(db_session, question):
    await corpus(db_session)

    found = await R.retrieve(db_session, question)

    assert not found.attempted
    assert found.totals is not None
    assert found.totals.open_total == 5
    assert found.totals.companies == 4
    assert found.totals.in_area == 3


async def test_without_how_many_it_is_still_not_searched(db_session):
    await corpus(db_session)

    found = await R.retrieve(db_session, "which jobs are available")

    assert found.unnamed
    assert found.totals is None


async def test_a_count_that_names_a_role_is_still_a_search(db_session):
    await corpus(db_session)

    found = await R.retrieve(db_session, "how many data engineer jobs are there")

    assert found.attempted
    assert found.totals is None


def test_the_facts_carry_each_count_with_its_scope():
    from apps.api.chat_facts import count_facts, render_totals

    found = R.Retrieval(
        attempted=False,
        unnamed=True,
        area="California on-site or remote",
        totals=R.OpenTotals(open_total=26263, companies=728, in_area=14512),
    )

    facts = {fact.key: fact for fact in count_facts(found)}

    assert facts["open_total"].value == 26263
    assert facts["open_total"].scope == "all open postings"
    assert facts["open_companies"].unit == "companies"
    assert facts["in_area"].value == 14512
    assert facts["in_area"].scope == "your search area"
    text = render_totals(found)
    assert "26263 open postings from 728 companies" in text
    assert "14512 of them are in your search area" in text


@pytest.mark.parametrize("route", ["/chat", "/chat/stream"])
async def test_the_reply_is_the_count_and_no_model_is_asked(
    worker_session, client, monkeypatch, route
):
    import apps.api.routers.chat as chat

    await corpus(worker_session)
    await worker_session.commit()

    class Refuses(StubProvider):
        async def complete(self, system, user, **kwargs):
            pytest.fail("A count is computed, never asked of a model")

    monkeypatch.setattr(chat.llm_router, "build_provider", lambda name: Refuses())

    response = await client.post(route, json={"message": ASKED})

    assert response.status_code == 200
    if route.endswith("stream"):
        frames = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        body = next(frame["reply"] for frame in frames if frame.get("type") == "done")
    else:
        body = response.json()
    assert body["provider"] == "facts"
    assert "5 open postings from 4 companies" in body["reply"]
    assert "3 of them are in your search area" in body["reply"]
    keys = {fact["key"]: fact["value"] for fact in body["count_facts"]}
    assert keys["open_total"] == 5
    assert keys["in_area"] == 3


async def test_a_follow_up_hands_the_model_the_totals_not_silence(worker_session):
    """With history the model answers; it must be given the numbers to repeat."""
    import apps.api.routers.chat as chat
    from packages.core.schemas import ChatRequest

    await corpus(worker_session)
    await worker_session.commit()

    ready = await chat.prepare(
        ChatRequest(
            message=ASKED,
            history=[{"question": "hi", "answer": "Hello."}],
        ),
        worker_session,
    )

    assert isinstance(ready, chat.Ready)
    assert "OPEN POSTINGS: 5 open postings from 4 companies" in ready.prompt
    assert "names no role, skill, company" not in ready.prompt


async def test_my_applications_keep_their_own_answer(worker_session, client, monkeypatch):
    import apps.api.routers.chat as chat

    await corpus(worker_session)
    await worker_session.commit()
    monkeypatch.setattr(chat.llm_router, "build_provider", lambda name: StubProvider())

    response = await client.post("/chat", json={"message": "how many applications do I have"})

    assert response.status_code == 200
    assert "open postings from" not in response.json()["reply"]
