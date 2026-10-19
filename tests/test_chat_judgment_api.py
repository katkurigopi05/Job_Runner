"""`/judgments`: serving the pool and recording the owner's grades (Phase 5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from packages.core.models import Company, Posting
from packages.core.models_judgments import RetrievalJudgment

NOW = datetime.now(UTC)


async def _pool(session) -> dict[str, RetrievalJudgment]:
    """Two dev questions and one held out, pooled with no grades."""
    company = Company(name="Acme", ats_type="greenhouse")
    session.add(company)
    await session.flush()
    rows = {}
    for n, (key, held_out, order) in enumerate(
        (
            ("kafka", False, 20),
            ("kafka", False, 10),
            ("rag", False, 5),
            ("london", True, 1),
        )
    ):
        posting = Posting(
            company_id=company.id,
            external_id=f"p{n}",
            url=f"https://x.test/p{n}",
            title=f"Engineer {n}",
            location="San Francisco, CA",
            description_raw="Kafka pipelines.",
            content_hash="h1",
            first_seen_at=NOW - timedelta(minutes=n),
        )
        session.add(posting)
        await session.flush()
        rows[f"{key}-{order}"] = RetrievalJudgment(
            set_version="chat-v1",
            question_key=key,
            question=f"{key} jobs",
            stratum="skill",
            held_out=held_out,
            posting_id=posting.id,
            pool_order=order,
            surfaced_by={"shipped": 1, "semantic": 3},
            content_hash="h0" if n == 0 else "h1",
        )
    session.add_all(rows.values())
    await session.commit()
    return rows


async def test_nothing_pooled_says_how_to_build_it(client: AsyncClient) -> None:
    summary = (await client.get("/judgments/summary")).json()

    assert summary["set_version"] is None and summary["total"] == 0
    assert "make judge-pool-write" in summary["notes"][0]
    assert (await client.get("/judgments/next")).json() == []


async def test_a_question_is_served_whole_in_its_shuffle_and_dev_first(
    client: AsyncClient, worker_session
) -> None:
    await _pool(worker_session)

    served = (await client.get("/judgments/next", params={"size": 10})).json()

    assert [(c["question_key"], c["position"], c["of"]) for c in served] == [
        ("kafka", 1, 2),
        ("kafka", 2, 2),
        ("rag", 1, 1),
        ("london", 1, 1),
    ]
    assert served[0]["question"] == "kafka jobs"
    assert served[0]["company"] == "Acme"


async def test_which_mode_found_a_posting_is_never_served(
    client: AsyncClient, worker_session
) -> None:
    """A grade given knowing which mode's answer it is leans toward that mode."""
    await _pool(worker_session)

    served = (await client.get("/judgments/next")).json()

    assert all("surfaced_by" not in c and "shipped" not in str(c) for c in served)


async def test_a_posting_edited_since_it_was_pooled_says_so(
    client: AsyncClient, worker_session
) -> None:
    rows = await _pool(worker_session)

    served = {c["id"]: c for c in (await client.get("/judgments/next")).json()}

    assert served[str(rows["kafka-20"].id)]["changed"] is True
    assert served[str(rows["kafka-10"].id)]["changed"] is False


async def test_a_grade_is_recorded_and_not_served_again(
    client: AsyncClient, worker_session
) -> None:
    rows = await _pool(worker_session)
    target = rows["kafka-10"].id

    response = await client.post(
        f"/judgments/{target}", json={"relevance": 2, "note": "says Kafka twice"}
    )

    assert response.status_code == 200
    assert response.json()["relevance"] == 2 and response.json()["graded_at"]
    served = [c["id"] for c in (await client.get("/judgments/next")).json()]
    assert str(target) not in served
    worker_session.expire_all()
    stored = await worker_session.scalar(
        select(RetrievalJudgment).where(RetrievalJudgment.id == target)
    )
    assert (stored.relevance, stored.note) == (2, "says Kafka twice")
    assert stored.surfaced_by == {"shipped": 1, "semantic": 3}, "the pool row is untouched"


async def test_grading_again_overwrites(client: AsyncClient, worker_session) -> None:
    rows = await _pool(worker_session)
    target = rows["rag-5"].id

    await client.post(f"/judgments/{target}", json={"relevance": 2})
    again = await client.post(f"/judgments/{target}", json={"relevance": 0})

    assert again.json()["relevance"] == 0


@pytest.mark.parametrize("relevance", [-1, 3])
async def test_a_grade_off_the_scale_is_refused(
    client: AsyncClient, worker_session, relevance: int
) -> None:
    rows = await _pool(worker_session)

    response = await client.post(f"/judgments/{rows['rag-5'].id}", json={"relevance": relevance})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


async def test_grading_something_not_pooled_is_not_found(client: AsyncClient) -> None:
    response = await client.post(
        "/judgments/00000000-0000-0000-0000-000000000000", json={"relevance": 1}
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_the_summary_counts_by_split_and_question(
    client: AsyncClient, worker_session
) -> None:
    rows = await _pool(worker_session)
    for key, grade in (("kafka-10", 2), ("kafka-20", 0), ("london-1", 1)):
        await client.post(f"/judgments/{rows[key].id}", json={"relevance": grade})

    summary = (await client.get("/judgments/summary")).json()

    assert (summary["total"], summary["graded"], summary["remaining"]) == (4, 3, 1)
    assert summary["by_grade"] == {"0": 1, "1": 1, "2": 1}
    assert summary["dev"] == {"total": 3, "graded": 2}
    assert summary["held_out"] == {"total": 1, "graded": 1}
    assert (summary["questions"], summary["questions_done"]) == (3, 2)
    assert summary["notes"] == []
