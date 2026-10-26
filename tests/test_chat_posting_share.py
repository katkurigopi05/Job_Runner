"""Counts and shares are facts computed over the same search area."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from packages.core.models import Company, Posting
from packages.llm.provider import StubProvider
from packages.matching import retrieve as R

QUESTION = "how many job postings on ai or ai ml engineer roles and whats the percentage"


@pytest.fixture(autouse=True)
def no_models(monkeypatch):
    monkeypatch.setattr(R, "get_reranker", lambda: None)
    monkeypatch.setattr(R, "_encoders", lambda *args: {})


async def posting(session, title, location="San Francisco, CA", body="A role.", closed=False):
    company = Company(name="ExampleCo " + uuid.uuid4().hex[:8], ats_type="greenhouse")
    session.add(company)
    await session.flush()
    row = Posting(
        company_id=company.id,
        title=title,
        location=location,
        url="https://jobs.example.test/" + uuid.uuid4().hex,
        description_raw=body,
        content_hash=uuid.uuid4().hex,
        closed_at=datetime.now(UTC) if closed else None,
    )
    session.add(row)
    await session.flush()
    return row


@pytest.mark.parametrize(
    "question",
    [
        QUESTION,
        "How many ML engineer jobs? What's the percentage?",
        "What percentage of jobs are AI engineer roles?",
    ],
)
async def test_count_request_is_not_an_extra_skill(db_session, question):
    await posting(db_session, "AI Engineer", body="Many percentage improvements.")
    await posting(db_session, "Machine Learning Engineer")
    await posting(db_session, "Data Engineer")
    found = await R.retrieve(db_session, question)
    assert found.role_total == 2
    assert found.beyond_role == ()
    assert all("mentions" not in kind for kind, _ in found.kinds)
    assert found.share.matching == 2
    assert found.share.open_total == 3
    assert found.share.percentage == 66.67


async def test_share_counts_all_in_area_open_postings_not_only_the_pool(db_session):
    await posting(db_session, "AI Engineer")
    await posting(db_session, "ML Engineer", "London, United Kingdom")
    await posting(db_session, "Data Engineer")
    await posting(db_session, "Designer", None)
    await posting(db_session, "Product Manager", "Boston, MA", "This is a remote role in the US.")
    await posting(db_session, "Data Analyst", "Boston, MA", "On-site only.")
    await posting(db_session, "AI Engineer", closed=True)
    found = await R.retrieve(db_session, QUESTION)
    assert found.share.matching == 1
    assert found.share.open_total == 4
    assert found.share.percentage == 25.0
    assert found.share.scope == "your search area"


async def test_no_percentage_query_does_not_add_a_corpus_count(db_session, monkeypatch):
    await posting(db_session, "AI Engineer")

    async def unexpected(*args, **kwargs):
        pytest.fail("A plain role question needs no percentage denominator scan")

    monkeypatch.setattr(R, "_matching_totals", unexpected)
    found = await R.retrieve(db_session, "how many AI engineer jobs")
    assert found.beyond_role == ()
    assert found.share is None


async def test_unknown_two_term_total_never_gets_a_percentage(db_session):
    from apps.api.routers.chat import _postings_section

    await posting(db_session, "Engineer", body="Kafka and Kubernetes.")
    found = await R.retrieve(db_session, "What percentage of jobs mention Kafka and Kubernetes?")
    assert found.match_total is None
    assert found.share is None
    assert "percentage unavailable" in _postings_section(found)


async def test_empty_corpus_does_not_divide_by_zero(db_session):
    found = await R.retrieve(db_session, QUESTION)
    assert found.share.open_total == 0
    assert found.share.matching == 0
    assert found.share.percentage is None


async def test_zero_role_matches_is_zero_percent_of_existing_area(db_session):
    await posting(db_session, "Data Engineer")
    found = await R.retrieve(db_session, QUESTION)
    assert found.share.percentage == 0.0
    assert found.share.open_total == 1


async def test_a_real_extra_subject_stays_searchable(db_session):
    await posting(db_session, "AI Engineer", body="Kafka infrastructure.")
    found = await R.retrieve(db_session, "How many AI engineer jobs using Kafka?")
    assert found.beyond_role == ("Kafka",)
    assert found.share is None


async def test_percentage_word_is_kept_when_it_is_the_subject(db_session):
    await posting(db_session, "AI Engineer", body="Percentage estimation.")
    found = await R.retrieve(db_session, "AI engineer roles involving percentage estimation")
    assert "percentage" in [term.lower() for term in found.beyond_role]


@pytest.mark.parametrize("route", ["/chat", "/chat/stream"])
async def test_computed_share_reaches_the_context_and_reply(
    worker_session, client, monkeypatch, route
):
    import json

    import apps.api.routers.chat as chat

    await posting(worker_session, "AI Engineer")
    await posting(worker_session, "Data Engineer")
    await worker_session.commit()

    class Recorder(StubProvider):
        async def complete(self, system, user, **kwargs):
            pytest.fail("Computed percentages must not ask a model")

    monkeypatch.setattr(chat.llm_router, "build_provider", lambda name: Recorder())
    from packages.core.schemas import ChatRequest

    ready = await chat.prepare(ChatRequest(message=QUESTION), worker_session)
    assert isinstance(ready, chat.Ready)
    response = await client.post(route, json={"message": QUESTION})
    assert response.status_code == 200
    if route.endswith("stream"):
        frames = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        body = next(frame["reply"] for frame in frames if frame.get("type") == "done")
    else:
        body = response.json()
    assert body["postings_share"] == {
        "matching": 1,
        "open_total": 2,
        "percentage": 50.0,
        "scope": "your search area",
    }
    assert body["provider"] == "facts"
    assert "50.00%" in body["reply"]
    assert "50.00%" in ready.prompt
    assert "1 of 2 open postings in your search area" in ready.prompt
    assert "mentions percentage" not in ready.prompt


@pytest.mark.parametrize("title", ["AI/ML Engineer", "Senior AI / ML Engineer", "AI ML Engineer"])
async def test_combined_ai_ml_title_names_the_same_role(db_session, title):
    row = await posting(db_session, title)
    found = await R.retrieve(db_session, QUESTION)
    assert found.role_total == 1
    assert found.passages[0].posting_id == row.id


async def test_denominator_is_not_limited_to_ranked_candidates(db_session):
    await posting(db_session, "AI Engineer")
    for index in range(125):
        await posting(db_session, f"Customer Service Representative {index}")
    found = await R.retrieve(db_session, QUESTION)
    assert found.share.matching == 1
    assert found.share.open_total == 126
    assert found.share.percentage == 0.79


async def test_bay_area_share_uses_a_separate_denominator_filter(db_session):
    await posting(db_session, "AI Engineer")
    await posting(db_session, "Data Engineer", "Oakland, CA")
    await posting(db_session, "AI Engineer", "United States, Remote")
    await posting(db_session, "Designer", "Los Angeles, CA")
    found = await R.retrieve(
        db_session, "How many AI engineer jobs in the bay area and what's the percentage?"
    )
    assert found.share.matching == 1
    assert found.share.open_total == 2
    assert found.share.percentage == 50.0
    assert found.share.scope == "the Bay Area"
    assert found.place_total == 1
    assert found.search_area_total == 2
    assert found.remote_or_unplaced_not_listed == 1


async def test_same_metadata_keeps_independent_remote_evidence_in_denominator(db_session):
    await posting(db_session, "AI Engineer")
    await posting(db_session, "Data Engineer", "Boston, MA", "Remote in the US.")
    await posting(db_session, "Data Engineer", "Boston, MA", "On-site only.")
    await posting(db_session, "Data Engineer", "Boston, MA", "Remote in the US.", closed=True)
    await posting(db_session, None, "Boston, MA", "Remote in the US.")
    found = await R.retrieve(db_session, QUESTION)
    assert found.share.open_total == 3
    assert found.share.percentage == 33.33
