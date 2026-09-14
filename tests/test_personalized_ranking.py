"""Skip reasons, explicit preferences, and the personalized order.

The property every test below protects: the base score is never rewritten.
Personalization is a second number, shown beside the first, with a reason for
every point it moves.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from packages.core.models import Company, Match, Posting
from packages.matching.personalize import MAX_TOTAL, adjust
from packages.matching.requirements import extract


class _Pref:
    def __init__(self, kind: str, value: str, weight: float) -> None:
        self.kind, self.value, self.weight = kind, value, weight


def _posting(title: str = "Backend Engineer", text: str = "Requirements\n- Python") -> Posting:
    return Posting(
        url="https://example.test/1",
        title=title,
        location="Austin, TX",
        description_raw=text,
        requirements_json=extract(text).as_json(),
    )


# --------------------------------------------------------------------------
# adjust
# --------------------------------------------------------------------------


def test_each_applied_preference_explains_itself() -> None:
    score, applied = adjust(
        0.6,
        _posting(),
        "Acme",
        [
            _Pref("skill", "python", 0.1),
            _Pref("company", "acme", -0.05),
            _Pref("skill", "rust", 0.2),
        ],
    )

    assert score == pytest.approx(0.65)
    assert {item.why for item in applied} == {"required skill: Python", "company is Acme"}


def test_the_total_adjustment_is_bounded() -> None:
    prefs = [_Pref("title_term", "backend", 0.3), _Pref("title_term", "engineer", 0.3)]

    score, _ = adjust(0.2, _posting(), None, prefs)

    assert score == pytest.approx(0.2 + MAX_TOTAL)


def test_scores_stay_within_zero_and_one() -> None:
    assert adjust(0.95, _posting(), None, [_Pref("title_term", "backend", 0.3)])[0] == 1.0
    assert adjust(0.05, _posting(), None, [_Pref("title_term", "backend", -0.3)])[0] == 0.0


def test_a_title_term_matches_words_not_fragments() -> None:
    _, applied = adjust(
        0.5, _posting(title="Backend Engineer"), None, [_Pref("title_term", "end", 0.1)]
    )

    assert applied == []


# --------------------------------------------------------------------------
# The API
# --------------------------------------------------------------------------


@pytest.fixture
async def feed(client: AsyncClient, worker_session, complete_candidate) -> dict:
    profile_id = uuid.UUID(complete_candidate["profile_id"])
    acme = Company(name="Acme Personal")
    beta = Company(name="Beta Personal")
    worker_session.add_all([acme, beta])
    await worker_session.flush()
    rows = {}
    for company, title, text, score in (
        (acme, "Backend Engineer A", "Requirements\n- Java", 0.8),
        (beta, "Backend Engineer B", "Requirements\n- Python", 0.7),
    ):
        posting = Posting(
            company_id=company.id,
            external_id=title,
            url=f"https://example.test/{company.name}",
            title=title,
            location="Remote — US",
            description_raw=text,
            requirements_json=extract(text).as_json(),
        )
        worker_session.add(posting)
        await worker_session.flush()
        match = Match(profile_id=profile_id, posting_id=posting.id, score=score, reasons_json={})
        worker_session.add(match)
        await worker_session.flush()
        rows[title[-1]] = str(match.id)
    await worker_session.commit()
    return {"profile_id": str(profile_id), **rows}


async def _feed(client: AsyncClient, feed: dict, **params) -> list[dict]:
    response = await client.get(
        "/matches", params={"profile_id": feed["profile_id"], "us_only": "false", **params}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_no_preferences_means_no_personalized_score(client, feed) -> None:
    rows = await _feed(client, feed, rank="personalized")

    assert [row["title"][-1] for row in rows] == ["A", "B"]
    assert all(row["personalized_score"] is None for row in rows)


async def test_a_preference_reorders_without_touching_the_base_score(client, feed) -> None:
    saved = await client.put(
        "/ranking/preferences", json={"kind": "skill", "value": "Python", "weight": 0.2}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["value"] == "python", "typed skills are stored as vocabulary keys"

    base = await _feed(client, feed)
    personal = await _feed(client, feed, rank="personalized")

    assert [row["title"][-1] for row in base] == ["A", "B"]
    assert [row["title"][-1] for row in personal] == ["B", "A"]
    by_title = {row["title"][-1]: row for row in personal}
    assert by_title["B"]["score"] == pytest.approx(0.7), "the base score is unchanged"
    assert by_title["B"]["personalized_score"] == pytest.approx(0.9)
    assert by_title["B"]["adjustments"][0]["why"] == "required skill: Python"


async def test_weights_outside_the_bound_are_refused(client) -> None:
    response = await client.put(
        "/ranking/preferences", json={"kind": "company", "value": "Acme", "weight": 0.9}
    )

    assert response.status_code == 400


async def test_an_unknown_kind_is_refused(client) -> None:
    response = await client.put(
        "/ranking/preferences", json={"kind": "vibes", "value": "good", "weight": 0.1}
    )

    assert response.status_code == 400


async def test_a_skip_records_its_reason_and_a_change_of_mind_clears_it(
    client, feed, committing_sessionmaker
) -> None:
    skipped = await client.post(
        f"/matches/{feed['A']}/decision", json={"decision": "skipped", "reason": "company"}
    )
    assert skipped.status_code == 200, skipped.text
    assert skipped.json()["skip_reason"] == "company"

    kept = await client.post(f"/matches/{feed['A']}/decision", json={"decision": "interested"})
    assert kept.json()["skip_reason"] is None

    async with committing_sessionmaker() as session:
        match = await session.scalar(select(Match).where(Match.id == uuid.UUID(feed["A"])))
    assert (match.decision, match.skip_reason) == ("interested", None)


async def test_a_reason_is_refused_on_interested_and_unknown_reasons(client, feed) -> None:
    on_interested = await client.post(
        f"/matches/{feed['A']}/decision", json={"decision": "interested", "reason": "salary"}
    )
    unknown = await client.post(
        f"/matches/{feed['A']}/decision", json={"decision": "skipped", "reason": "bad vibes"}
    )

    assert on_interested.status_code == 400
    assert unknown.status_code == 400


async def test_repeated_company_skips_become_a_suggestion_not_a_preference(
    client, worker_session, complete_candidate
) -> None:
    profile_id = uuid.UUID(complete_candidate["profile_id"])
    company = Company(name="Skipped Often")
    worker_session.add(company)
    await worker_session.flush()
    for index in range(3):
        posting = Posting(
            company_id=company.id, external_id=str(index), url=f"https://s.test/{index}", title="x"
        )
        worker_session.add(posting)
        await worker_session.flush()
        worker_session.add(
            Match(
                profile_id=profile_id,
                posting_id=posting.id,
                score=0.5,
                decision="skipped",
                skip_reason="company",
            )
        )
    await worker_session.commit()

    suggestions = (await client.get("/ranking/suggestions")).json()

    assert suggestions == [
        {
            "kind": "company",
            "value": "Skipped Often",
            "weight": -0.1,
            "evidence": "skipped 3 postings at Skipped Often for the company",
        }
    ]
    assert (await client.get("/ranking/preferences")).json() == [], "nothing applied by itself"
