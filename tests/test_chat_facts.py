"""Synthetic regressions for factual answers and their count scopes."""

from __future__ import annotations

import json
import uuid

import pytest
from pydantic import ValidationError

from packages.matching.retrieve import Passage, PostingShare, Retrieval


def _passage() -> Passage:
    return Passage(
        "P1",
        uuid.uuid4(),
        "Data Engineer",
        "Example Labs",
        "San Francisco",
        "https://example.test/job",
        "Python tooling.",
        None,
    )


def _facts():
    from apps.api import chat_facts

    return chat_facts


def test_role_total_and_subject_counts_are_different_facts():
    found = Retrieval(
        attempted=True,
        role_total=75,
        roles=("data engineer",),
        beyond_role=("remote",),
        beyond_in_title=2,
        beyond_mentioned=31,
    )
    counts = {f.key: f for f in _facts().count_facts(found)}
    assert counts["role_total"].value == 75
    assert counts["subject_title"].value == 2
    assert counts["subject_description"].value == 31
    assert counts["role_total"].predicate != counts["subject_description"].predicate


def test_unknown_match_count_is_not_zero():
    counts = {
        f.key: f
        for f in _facts().count_facts(
            Retrieval(attempted=True, searched=120, passages=(_passage(),))
        )
    }
    assert counts["matched"].value is None
    assert counts["matched"].coverage == "unknown"
    text = _facts().render_postings(Retrieval(attempted=True, searched=120, passages=(_passage(),)))
    assert "total matching count is unknown" in text
    assert "1 open posting" not in text


@pytest.mark.parametrize(
    "value,coverage", [(0, "unknown"), (None, "exact"), (-1, "exact"), (True, "exact")]
)
def test_invalid_count_contract_is_refused(value, coverage):
    from packages.core.schemas import ChatCountFact

    with pytest.raises(ValidationError):
        ChatCountFact(
            key="matched",
            value=value,
            coverage=coverage,
            unit="postings",
            predicate="matching",
            scope="search area",
        )


def test_renderer_preserves_remote_mentions_and_the_rest():
    text = _facts().render_postings(
        Retrieval(
            attempted=True,
            searched=120,
            passages=(_passage(),),
            role_total=75,
            roles=("data engineer",),
            beyond_role=("remote",),
            beyond_in_title=2,
            beyond_mentioned=31,
        )
    )
    assert "75" in text and "2" in text and "31" in text and "42" in text
    assert "75 remote" not in text
    assert "remote eligibility" in text


def test_renderer_cannot_invent_a_compound_percentage():
    text = _facts().render_postings(
        Retrieval(
            attempted=True,
            searched=120,
            role_total=75,
            roles=("data engineer",),
            share_requested=True,
        )
    )
    assert "percentage unavailable" in text.lower()
    assert "complete matching count" in text
    assert "120" in text


def test_renderer_repeats_computed_share_and_denominator():
    text = _facts().render_postings(
        Retrieval(
            attempted=True,
            searched=120,
            role_total=3,
            roles=("AI / ML engineer",),
            share=PostingShare(3, 100, "your search area"),
        )
    )
    assert "3 of 100" in text
    assert "3.00%" in text


def test_bay_area_omission_notice_is_mandatory():
    text = _facts().render_postings(
        Retrieval(
            attempted=True,
            searched=120,
            role_total=22,
            roles=("data engineer",),
            place_total=22,
            place_counts_complete=True,
            search_area_total=75,
            remote_or_unplaced_not_listed=50,
        )
    )
    assert "in the Bay Area: 22" in text
    assert "50 remote or unplaced roles" in text
    assert "75" in text
    counts = {
        f.key: f
        for f in _facts().count_facts(
            Retrieval(
                attempted=True,
                searched=120,
                place_total=22,
                place_counts_complete=True,
                remote_or_unplaced_not_listed=50,
            )
        )
    }
    assert counts["remote_or_unplaced"].scope != counts["place_total"].scope
    assert counts["searched"].scope != counts["place_total"].scope


def test_partial_place_count_does_not_claim_an_exhaustive_area_total():
    text = _facts().render_postings(
        Retrieval(
            attempted=True,
            place_total=2,
            place_counts_complete=False,
            search_area_total=8,
            remote_or_unplaced_not_listed=5,
        )
    )
    assert "8 matching postings among retrieved candidates" in text


def test_company_application_facts_do_not_use_global_counts():
    facts = _facts()
    application = facts.ApplicationFact(
        title="Systems Engineer",
        url="https://example.test/jobs/999999",
        status="needs_review",
        failure_reason=None,
        created="2000-01-01",
        updated="2000-01-02",
    )
    company = facts.CompanyApplications(name="Example Labs", applications=(application,))
    text = company.answer()
    assert "1 application at Example Labs" in text
    assert "needs_review" in text and "Systems Engineer" in text
    assert "2000-01-01" in text and "2000-01-02" in text
    assert (
        facts.CompanyApplications(name="Example Labs", applications=()).answer()
        == "No applications at Example Labs."
    )


def test_owner_counts_keep_waiting_actions_separate_from_statuses():
    facts = _facts()
    from packages.core.notify import ParkReason

    owner = facts.OwnerFacts(
        context="unused",
        statuses=(("failed", 4), ("needs_review", 1)),
        waiting=((ParkReason.REVIEW, 1), (ParkReason.MANUAL, 3)),
    )
    text = owner.answer()
    assert "5 applications" in text
    assert "review: 1" in text and "manual_completion_required: 3" in text
    assert "already counted" in text


def test_keyword_count_does_not_mean_required():
    passage = _passage()
    text = _facts().render_postings(
        Retrieval(
            attempted=True,
            searched=120,
            passages=(passage,),
            match_total=98,
            company_total=59,
            matched_term="Kafka",
        ),
        {passage.posting_id: ["Kafka here: preferred, not required"]},
    )
    assert "98 postings at 59 companies" in text
    assert "mention" in text
    assert "preferred, not required" in text
    assert "closest 1" in text
    assert "[P1]" in text


@pytest.mark.parametrize("stream", [False, True])
async def test_both_routes_return_fixed_facts_without_a_model(monkeypatch, stream):
    from apps.api.routers import chat, chat_stream
    from packages.core.schemas import ChatRequest

    ready = chat.Ready(
        selected="kaggle",
        include_mail=False,
        prompt="private context",
        found=Retrieval(attempted=True, searched=120),
        factual_answer="Computed facts.",
    )

    async def prepared(*args, **kwargs):
        return ready

    def forbidden(*args, **kwargs):
        pytest.fail("A fixed factual answer must not create a provider")

    monkeypatch.setattr(chat, "prepare", prepared)
    monkeypatch.setattr(chat_stream, "_prepared", prepared)
    monkeypatch.setattr(chat, "provider_for", forbidden)
    monkeypatch.setattr(chat_stream, "provider_for", forbidden)
    request = ChatRequest(message="How many jobs are open?", provider="kaggle")
    if stream:
        response = await chat_stream.chat_stream(request)
        frames = [json.loads(part) async for part in response.body_iterator]
        assert [f["type"] for f in frames] == ["done"]
        reply = frames[0]["reply"]
    else:
        reply = (await chat.chat(request, None)).model_dump(mode="json")
    assert reply["reply"] == "Computed facts."
    assert reply["provider"] == "facts"
    assert reply["model"] is None and reply["local"] is True
    assert reply["shared_mail"] is False and reply["mail_in_context"] is False


@pytest.mark.parametrize("route", ["/chat", "/chat/stream"])
async def test_real_preparation_renders_company_status_without_provider(
    client, worker_session, monkeypatch, route
):
    from apps.api.routers import chat
    from tests.test_chat_company_applications import _setup

    await _setup(worker_session)

    def forbidden(*args, **kwargs):
        pytest.fail("Company application status must be copied, not generated")

    monkeypatch.setattr(chat.llm_router, "build_provider", forbidden)
    response = await client.post(
        route,
        json={"message": "What's the status of my Cloudflare application?", "provider": "kaggle"},
    )
    assert response.status_code == 200
    reply = (
        next(
            json.loads(line)["reply"]
            for line in response.text.splitlines()
            if json.loads(line)["type"] == "done"
        )
        if route.endswith("stream")
        else response.json()
    )
    assert reply["provider"] == "facts"
    assert "1 application at Cloudflare" in reply["reply"]
    assert "Systems Engineer" in reply["reply"]
    assert "needs_review" in reply["reply"]
    assert "2000-01-01" in reply["reply"]
    assert "/other/" not in reply["reply"]


@pytest.mark.parametrize(
    "question",
    [
        "Which jobs use market share data?",
        "Jobs developing percentage estimation tools",
        "Jobs building total rewards software",
    ],
)
def test_subject_words_are_not_count_requests(question):
    assert not _facts().asks_for_counts(question)


@pytest.mark.parametrize(
    "question,expected",
    [
        ("What's the status of my application?", True),
        ("How many applications do I have?", True),
        ("What needs me?", True),
        ("Which questions is my application parked on?", False),
        ("Why is my application waiting for review?", False),
        ("Did my application get a reply?", False),
    ],
)
def test_application_snapshot_does_not_replace_record_details(question, expected):
    assert _facts().asks_for_application_snapshot(question) is expected
