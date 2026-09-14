"""The match feed filtered on pay, skills and education, end to end.

Postings are stored the way the crawler stores them — columns and the JSON
reading both filled by `requirements.extract` — so this exercises the route,
the filter, and the stored shape together rather than any one in isolation.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Match, Posting
from packages.matching.requirements import EXTRACTOR_VERSION, extract

ROWS = [
    (
        "Backend Engineer A",
        "Requirements\n- Python\n- Bachelor's degree in Computer Science\n"
        "The annual base salary range is $140,000 - $180,000 USD.",
        0.9,
    ),
    ("Platform Engineer B", "Nice to have\n- Kubernetes", 0.8),
    (
        "Contractor C",
        "Requirements\n- Java\n- Master's degree required\nPay: $60 - $75 per hour",
        0.7,
    ),
]


@pytest.fixture
async def feed(client: AsyncClient, worker_session: AsyncSession, complete_candidate) -> str:
    profile_id = uuid.UUID(complete_candidate["profile_id"])
    for index, (title, body, score) in enumerate(ROWS):
        reading = extract(body)
        pay = reading.compensation
        posting = Posting(
            url=f"https://boards.greenhouse.io/acme/jobs/r{index}",
            title=title,
            location="Remote — US",
            description_raw=body,
            salary_min=pay.minimum if pay else None,
            salary_max=pay.maximum if pay else None,
            salary_currency=pay.currency if pay else None,
            salary_period=pay.period if pay else None,
            requirements_json=reading.as_json(),
            requirements_version=EXTRACTOR_VERSION,
        )
        worker_session.add(posting)
        await worker_session.flush()
        worker_session.add(
            Match(profile_id=profile_id, posting_id=posting.id, score=score, reasons_json={})
        )
    await worker_session.commit()
    return str(profile_id)


async def _titles(client: AsyncClient, profile_id: str, **params) -> list[str]:
    params.setdefault("us_only", "false")
    response = await client.get("/matches", params={"profile_id": profile_id, **params})
    assert response.status_code == 200, response.text
    return [row["title"][-1] for row in response.json()]


async def test_a_pay_floor_keeps_only_stated_pay_that_reaches_it(client, feed) -> None:
    assert await _titles(client, feed, min_salary=150_000) == ["A"]


async def test_widening_pay_brings_back_the_unstated_and_the_unconvertible(client, feed) -> None:
    titles = await _titles(client, feed, min_salary=150_000, include_unknown_salary="true")

    assert titles == ["A", "B", "C"]


async def test_a_lacking_required_skill_excludes_only_the_posting_that_requires_it(
    client, feed
) -> None:
    assert await _titles(client, feed, lacking_skills="java") == ["A", "B"]


async def test_an_education_bound_excludes_higher_and_unstated(client, feed) -> None:
    assert await _titles(client, feed, max_education="bachelor") == ["A"]
    assert await _titles(
        client, feed, max_education="bachelor", include_unknown_education="true"
    ) == ["A", "B"]


async def test_a_skill_outside_the_vocabulary_is_refused_not_ignored(client, feed) -> None:
    response = await client.get(
        "/matches", params={"profile_id": feed, "lacking_skills": "fortran77"}
    )

    assert response.status_code == 400
    assert "fortran77" in response.json()["error"]["message"]


async def test_the_card_carries_pay_and_requirements_with_evidence(client, feed) -> None:
    response = await client.get("/matches", params={"profile_id": feed, "us_only": "false"})
    rows = {row["title"][-1]: row for row in response.json()}

    assert rows["A"]["compensation"]["maximum"] == 180_000
    assert "$140,000 - $180,000" in rows["A"]["compensation"]["quote"]
    assert rows["B"]["compensation"] is None
    assert "compensation" in rows["B"]["requirements"]["unknown"]
    assert rows["C"]["requirements"]["education"]["level"] == "master"
