"""The analytics routes, over HTTP.

`test_analytics.py` covers the reports themselves — it calls `funnel.build`,
`cadence.silence` and `digest.build` directly, and it is the right place for
the arithmetic. What it cannot see is the layer above: whether the declared
`response_model` describes what the handler actually returns.

That gap is not hypothetical here. `POST /matches/{id}/decision` shipped
declaring `response_model=MatchOut` while returning a `Match`, and every call
would have failed response validation — invisible for as long as nothing
exercised the route. Two of these three endpoints have no caller in the web
app either, so the same silence applies to them.

So these tests are deliberately shallow on arithmetic and specific about the
boundary: status codes, the declared shape, and the query bounds.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.enums import ApplicationStatus, Outcome
from packages.core.models import Application, Posting

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def answered_application(
    client: AsyncClient, worker_session: AsyncSession, complete_candidate
) -> dict:
    """One submitted application the employer answered, and one still waiting."""
    candidate_id = uuid.UUID(complete_candidate["candidate_id"])
    profile_id = uuid.UUID(complete_candidate["profile_id"])
    now = datetime.now(UTC)

    posting = Posting(url="https://boards.greenhouse.io/acme/jobs/9", title="Backend Engineer")
    worker_session.add(posting)
    await worker_session.flush()

    answered = Application(
        candidate_id=candidate_id,
        profile_id=profile_id,
        posting_id=posting.id,
        url="https://boards.greenhouse.io/acme/jobs/9",
        ats="greenhouse",
        status=ApplicationStatus.SUBMITTED,
        outcome=Outcome.REJECTED,
        outcome_at=now - timedelta(days=3),
        created_at=now - timedelta(days=20),
    )
    waiting = Application(
        candidate_id=candidate_id,
        profile_id=profile_id,
        url="https://boards.greenhouse.io/acme/jobs/10",
        ats="greenhouse",
        status=ApplicationStatus.SUBMITTED,
        created_at=now - timedelta(days=40),
    )
    worker_session.add_all([answered, waiting])
    await worker_session.commit()
    return {"profile_id": str(profile_id)}


#: The top-level keys each report promises. Checking the *set* rather than
#: "it returned a dict" is what makes this catch a wrong `response_model`:
#: every one of these models has all-default fields, so the wrong one still
#: serializes happily and only the field names give it away.
REPORT_KEYS = {
    "/analytics/funnel": {"total", "submitted", "answer_rate", "score_tracks_outcome"},
    "/analytics/cadence": {"silent", "due", "stale", "latency"},
    "/analytics/digest": {"window_days", "postings_seen", "quiet_week", "funnel", "latency"},
}


@pytest.mark.parametrize("path", list(REPORT_KEYS))
async def test_every_report_returns_the_shape_it_declares(client: AsyncClient, path: str) -> None:
    """A handler whose `response_model` does not describe what it returns fails here.

    Run against an empty database on purpose: the shape has to hold before
    there is any data to hide a mismatch behind.
    """
    response = await client.get(path)

    assert response.status_code == 200, response.text
    assert REPORT_KEYS[path] <= set(response.json())


@pytest.mark.parametrize("path", list(REPORT_KEYS))
async def test_every_report_survives_a_profile_filter(
    client: AsyncClient, path: str, answered_application
) -> None:
    response = await client.get(path, params={"profile_id": answered_application["profile_id"]})

    assert response.status_code == 200, response.text
    assert REPORT_KEYS[path] <= set(response.json())


async def test_an_empty_funnel_reports_null_rather_than_zero(client: AsyncClient) -> None:
    """0% reads as "every employer ignored you"; null is the honest empty answer."""
    body = (await client.get("/analytics/funnel")).json()

    assert body["total"] == 0
    assert body["answer_rate"] is None
    assert body["engagement_rate"] is None
    assert body["score_tracks_outcome"] is None


async def test_the_cadence_route_carries_its_latency(
    client: AsyncClient, answered_application
) -> None:
    """`latency` is a nested model — the one place a mapper is most likely to drop a field.

    Asserting the *values* rather than the keys is the point. A mapper that
    substitutes a default `LatencyOut()` keeps every key and reports that no
    employer has ever answered, which is a plausible lie of exactly the kind
    `test_analytics.py`'s docstring is worried about.
    """
    body = (await client.get("/analytics/cadence")).json()

    assert set(body) == {"silent", "due", "stale", "latency"}

    latency = body["latency"]
    assert latency["samples"] == 1, "the answered application should be one latency sample"
    assert latency["median_days"] is not None
    assert latency["median_rejection_days"] is not None


async def test_the_digest_carries_both_nested_reports(
    client: AsyncClient, answered_application
) -> None:
    body = (await client.get("/analytics/digest")).json()

    assert body["window_days"] == 7
    assert isinstance(body["funnel"], dict)
    assert isinstance(body["latency"], dict)


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/analytics/digest", {"window_days": 0}),
        ("/analytics/digest", {"window_days": 366}),
        ("/analytics/cadence", {"silent_after_days": 0}),
        ("/analytics/cadence", {"silent_after_days": 181}),
    ],
)
async def test_out_of_range_windows_are_refused(
    client: AsyncClient, path: str, params: dict
) -> None:
    """An unbounded window is a full-table scan the owner asked for by accident."""
    response = await client.get(path, params=params)

    # §10: validation failures come back in the shared envelope as 400
    # invalid_request, not FastAPI's bare 422.
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_request"
