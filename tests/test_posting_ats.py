"""§23's "before" score, at the point the owner is still choosing.

`/review` has carried an ATS score since tailoring landed, but only once an
application exists — by which point the slot is already spent. `/matches` had
a rubric saying how well the job fits the owner and nothing saying how well
their résumé reads to the machine that screens it.

The load-bearing property is *which* résumé gets scored: the one
`choose_base_resume` would actually start from, not the profile's default.
Those diverge the moment the owner has more than one base résumé, and a score
on screen for a file the employer would never receive is the defect CLAUDE.md
§15 records twice.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Posting

BACKEND_POSTING = """
Staff Backend Engineer

Requirements:
- 8+ years building distributed services in Python
- Deep experience with PostgreSQL and Kubernetes
- Strong background in API design and observability
"""


@pytest.fixture
async def posting(worker_session: AsyncSession) -> Posting:
    row = Posting(
        url="https://boards.greenhouse.io/acme/jobs/1",
        title="Staff Backend Engineer",
        location="Remote - US",
        description_raw=BACKEND_POSTING,
    )
    worker_session.add(row)
    await worker_session.commit()
    await worker_session.refresh(row)
    return row


async def test_it_scores_the_resume_against_the_posting(
    client: AsyncClient, complete_candidate, posting: Posting
) -> None:
    response = await client.get(
        f"/postings/{posting.id}/ats",
        params={"profile_id": complete_candidate["profile_id"]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert 0.0 <= body["parse"] <= 1.0
    assert 0.0 <= body["keywords"] <= 1.0
    assert body["scored_against_posting"] is True
    assert body["posting_id"] == str(posting.id)


async def test_it_names_the_resume_it_scored_and_why(
    client: AsyncClient, complete_candidate, posting: Posting
) -> None:
    """A score with no stated subject cannot be checked against anything."""
    response = await client.get(
        f"/postings/{posting.id}/ats",
        params={"profile_id": complete_candidate["profile_id"]},
    )

    body = response.json()
    assert uuid.UUID(body["resume_id"])
    assert isinstance(body["resume_version"], int)
    assert body["resume_reason"], "the pick must explain itself"


async def test_a_posting_with_no_description_reports_keywords_as_unscored(
    client: AsyncClient, complete_candidate, worker_session: AsyncSession
) -> None:
    """0.0 there means "not asked", and must not read as "matches nothing".

    The same distinction `AtsReport.scored_against_posting` exists for. A
    crawled posting whose body never came through is common, and rendering it
    as 0% coverage would tell the owner their résumé is hopeless for a job
    nothing was ever compared against.
    """
    bare = Posting(url="https://boards.greenhouse.io/acme/jobs/2", title="Engineer")
    worker_session.add(bare)
    await worker_session.commit()
    await worker_session.refresh(bare)

    response = await client.get(
        f"/postings/{bare.id}/ats",
        params={"profile_id": complete_candidate["profile_id"]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scored_against_posting"] is False
    assert body["parse"] > 0.0, "parse is still meaningful with no posting"


async def test_missing_terms_are_reported_but_never_as_a_to_do_list(
    client: AsyncClient, complete_candidate, posting: Posting
) -> None:
    """§2.1 — these are what the posting asks and the résumé cannot back.

    Reported so the owner can decide whether the job is worth applying to.
    Writing them *into* the résumé would be fabrication, which is why the
    field is `missing` and not `suggested`.
    """
    response = await client.get(
        f"/postings/{posting.id}/ats",
        params={"profile_id": complete_candidate["profile_id"]},
    )

    body = response.json()
    assert isinstance(body["missing"], list)
    assert isinstance(body["supported"], list)
    assert not set(body["missing"]) & set(body["supported"]), (
        "a term cannot be both backed and missing"
    )


async def test_findings_carry_their_cost_and_the_offending_line(
    client: AsyncClient, complete_candidate, posting: Posting
) -> None:
    response = await client.get(
        f"/postings/{posting.id}/ats",
        params={"profile_id": complete_candidate["profile_id"]},
    )

    for finding in response.json()["findings"]:
        assert finding["code"]
        assert finding["detail"]
        assert finding["cost"] >= 0.0


async def test_an_unknown_posting_is_not_found(client: AsyncClient, complete_candidate) -> None:
    response = await client.get(
        f"/postings/{uuid.uuid4()}/ats",
        params={"profile_id": complete_candidate["profile_id"]},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_an_unknown_profile_is_not_found(client: AsyncClient, posting: Posting) -> None:
    response = await client.get(
        f"/postings/{posting.id}/ats", params={"profile_id": str(uuid.uuid4())}
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_a_profile_with_no_resume_says_so_rather_than_scoring_nothing(
    client: AsyncClient, posting: Posting
) -> None:
    """An empty score would read as a terrible résumé rather than no résumé."""
    suffix = uuid.uuid4().hex[:8]
    cand = await client.post(
        "/candidates", json={"name": "No Resume", "email": f"nr-{suffix}@example.com"}
    )
    prof = await client.post(
        "/profiles",
        json={
            "candidate_id": cand.json()["id"],
            "label": "bare",
            "phone": "+1-555-0101",
            "location": "Austin, TX",
            "work_auth": "US citizen",
            "needs_sponsorship": False,
        },
    )

    response = await client.get(
        f"/postings/{posting.id}/ats", params={"profile_id": prof.json()["id"]}
    )

    assert response.status_code == 400
    assert "base résumé" in response.json()["error"]["message"]
