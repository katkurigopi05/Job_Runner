"""Rates and charts in the assistant come from code, never from the model.

The model may repeat a number its context states and may not derive one
(`assistant.system`). So a question about a reply rate gets the rate computed
from the funnel and handed over as a line, and a request for a chart is
answered in code before any model is chosen, with the chart's data on the reply
for the dock to draw. A posting share is #221's (`postings_share`); this is
the owner's own applications.
"""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient

from apps.api.chat_figures import (
    ChartSubject,
    asks_for_application_rates,
    asks_for_chart,
    chart_subject,
    rates_section,
)
from packages.core.enums import ApplicationStatus, Outcome
from tests.test_analytics import _application, _owner
from tests.test_chat_grounding import _answers

SUBMITTED = ApplicationStatus.SUBMITTED.value
FAILED = ApplicationStatus.FAILED.value


# --------------------------------------------------------------------------
# What is asked
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "chart my applications by status",
        "Can you graph my applications per week?",
        "plot reply rate by match score",
        "visualize my pipeline",
        "show me a bar chart of my applications",
    ],
)
def test_a_chart_request_is_recognised(question: str) -> None:
    assert asks_for_chart(question), question


@pytest.mark.parametrize(
    "question",
    [
        "What needs me?",
        "Which jobs mention Kafka?",
        "what is my interview rate",
        "Find data visualization engineer jobs",
        "Any roles with charting libraries like D3?",
    ],
)
def test_other_questions_are_not_chart_requests(question: str) -> None:
    assert not asks_for_chart(question), question


@pytest.mark.parametrize(
    ("question", "subject"),
    [
        ("chart my applications by status", ChartSubject.STATUS),
        ("graph my pipeline", ChartSubject.STATUS),
        ("chart my applications", ChartSubject.STATUS),
        ("plot my applications per week", ChartSubject.WEEKLY),
        ("graph applications over time", ChartSubject.WEEKLY),
        ("chart reply rate by match score", ChartSubject.SCORE),
        ("plot interviews by score", ChartSubject.SCORE),
        ("graph postings by company", None),
        ("chart my skills", None),
    ],
)
def test_a_chart_has_a_subject_it_can_draw_or_none(
    question: str, subject: ChartSubject | None
) -> None:
    assert chart_subject(question) is subject


@pytest.mark.parametrize(
    "question",
    [
        "what is my interview rate?",
        "What percentage of my applications got a reply?",
        "how often do I hear back?",
        "whats my response rate",
        "what share of my applications were rejected",
        "reply rate by match score?",
    ],
)
def test_an_application_rate_question_is_recognised(question: str) -> None:
    assert asks_for_application_rates(question), question


@pytest.mark.parametrize(
    "question",
    [
        # A posting share is #221's: "percentage" with no application in it.
        "what percentage of postings are AI engineer roles",
        "how many applications do I have",
        "Which jobs have a high success rate of hiring juniors?",
        "What needs me?",
    ],
)
def test_other_questions_are_not_application_rate_questions(question: str) -> None:
    assert not asks_for_application_rates(question), question


# --------------------------------------------------------------------------
# The rates, computed
# --------------------------------------------------------------------------


async def _six_submitted_two_failed(session) -> None:
    candidate, profile = await _owner(session)
    for outcome in (
        Outcome.REJECTED.value,
        Outcome.REJECTED.value,
        Outcome.INTERVIEW.value,
        None,
        None,
        Outcome.ACKNOWLEDGED.value,
    ):
        await _application(session, candidate, profile, score=0.85, outcome=outcome)
    for _ in range(2):
        await _application(session, candidate, profile, status=FAILED)


async def test_rates_are_counts_with_their_denominators(db_session) -> None:
    await _six_submitted_two_failed(db_session)

    section = await rates_section(db_session)

    assert section.startswith("RATES")
    assert "applications: 8 in all" in section
    assert "submitted 6 of 8 (75%)" in section
    assert "failed 2 of 8 (25%)" in section
    # An automated receipt is not a reply.
    assert "any reply from an employer: 3 of 6 submitted (50%)" in section
    assert "a person engaged (interview, offer, request, assessment): 1 of 6 submitted (17%)" in (
        section
    )
    assert "0.80–1.00: 1 of 6 submitted engaged (17%)" in section


async def test_too_few_submitted_states_counts_and_withholds_the_rate(db_session) -> None:
    candidate, profile = await _owner(db_session)
    for outcome in (Outcome.INTERVIEW.value, None, None):
        await _application(db_session, candidate, profile, score=0.7, outcome=outcome)

    section = await rates_section(db_session)

    assert "any reply from an employer: 1 of 3 submitted" in section
    assert "too few to read a rate" in section
    assert "(33%)" not in section


async def test_no_applications_is_said_not_shown_as_zero(db_session) -> None:
    section = await rates_section(db_session)

    assert "no applications yet" in section
    assert "0%" not in section


# --------------------------------------------------------------------------
# A rate question reaches the model with the rates; the model computes none
# --------------------------------------------------------------------------


async def test_a_rate_question_hands_the_model_the_computed_rates(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    await _six_submitted_two_failed(worker_session)
    await worker_session.commit()
    seen = _answers(monkeypatch, "Your reply rate is 50%: 3 of 6 submitted.")

    body = (
        await client.post(
            "/chat", json={"message": "What percentage of my applications got a reply?"}
        )
    ).json()

    assert body["provider"] != "chart"
    assert "RATES" in seen["user"]
    assert "3 of 6 submitted (50%)" in seen["user"]
    # #221's posting-share line would contradict it: this was not a posting question.
    assert "percentage unavailable" not in seen["user"]
    assert "RATES" in seen["system"]


async def test_an_ordinary_question_gets_no_rates(client: AsyncClient, monkeypatch) -> None:
    seen = _answers(monkeypatch, "Nothing needs you.")

    await client.post("/chat", json={"message": "Explain what you can help with"})

    assert "RATES" not in seen["user"]


# --------------------------------------------------------------------------
# A chart is answered in code, with its data
# --------------------------------------------------------------------------


def _never_ask_a_model(monkeypatch) -> None:
    import apps.api.routers.chat as chat_module

    def refuse(name=None):
        raise AssertionError("a chart must not reach a model")

    monkeypatch.setattr(chat_module.llm_router, "build_provider", refuse)


async def test_a_status_chart_is_drawn_from_the_database(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    await _six_submitted_two_failed(worker_session)
    await worker_session.commit()
    _never_ask_a_model(monkeypatch)

    body = (await client.post("/chat", json={"message": "chart my applications by status"})).json()

    assert body["provider"] == "chart"
    assert body["local"] is True
    chart = body["chart"]
    assert chart["unit"] == "applications"
    assert {(p["label"], p["value"]) for p in chart["points"]} == {
        ("submitted", 6),
        ("failed", 2),
    }
    assert "8" in body["reply"]


async def test_a_weekly_chart_has_twelve_weeks_and_counts_every_application(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    candidate, profile = await _owner(worker_session)
    for days in (0, 8, 8, 20, 200):
        await _application(worker_session, candidate, profile, created_days_ago=days)
    await worker_session.commit()
    _never_ask_a_model(monkeypatch)

    body = (await client.post("/chat", json={"message": "plot my applications per week"})).json()

    points = body["chart"]["points"]
    assert len(points) == 12
    # The one from 200 days ago is outside the twelve weeks, and the caption says so.
    assert sum(p["value"] for p in points) == 4
    assert "12 weeks" in body["chart"]["caption"]


async def test_a_score_chart_marks_bands_too_small_to_read(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    candidate, profile = await _owner(worker_session)
    for outcome in (Outcome.INTERVIEW.value, None, None, None, None):
        await _application(worker_session, candidate, profile, score=0.9, outcome=outcome)
    await _application(worker_session, candidate, profile, score=0.55, outcome=None)
    await worker_session.commit()
    _never_ask_a_model(monkeypatch)

    body = (await client.post("/chat", json={"message": "chart reply rate by match score"})).json()

    points = {p["label"]: p for p in body["chart"]["points"]}
    assert body["chart"]["unit"] == "%"
    assert points["0.80–1.00"]["value"] == 20.0
    assert points["0.80–1.00"]["reliable"] is True
    assert points["0.80–1.00"]["note"] == "1 of 5 submitted"
    assert points["0.50–0.65"]["reliable"] is False
    assert "too few" in body["chart"]["caption"]


async def test_a_chart_it_cannot_draw_says_what_it_can(client: AsyncClient, monkeypatch) -> None:
    _never_ask_a_model(monkeypatch)

    body = (await client.post("/chat", json={"message": "graph postings by company"})).json()

    assert body["provider"] == "chart"
    assert body["chart"] is None
    assert "by status" in body["reply"] and "per week" in body["reply"]


async def test_no_applications_means_no_chart(client: AsyncClient, monkeypatch) -> None:
    _never_ask_a_model(monkeypatch)

    body = (await client.post("/chat", json={"message": "chart my applications"})).json()

    assert body["chart"] is None
    assert "no applications yet" in body["reply"].lower()


async def test_the_stream_carries_the_chart_in_its_one_frame(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    await _six_submitted_two_failed(worker_session)
    await worker_session.commit()
    _never_ask_a_model(monkeypatch)

    response = await client.post("/chat/stream", json={"message": "graph my pipeline"})
    frames = [json.loads(line) for line in response.text.splitlines() if line.strip()]

    assert [frame["type"] for frame in frames] == ["done"]
    assert frames[0]["reply"]["chart"]["points"]
