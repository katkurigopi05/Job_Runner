"""Exact repeat listings occupy one presentation place without changing search ranks."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from packages.core.models import Company, Posting
from packages.matching.retrieve import retrieve


@pytest.mark.parametrize("change", ["none", "text", "title", "company"])
async def test_repeats_collapse_only_identical_jobs(worker_session, change):
    company = Company(name="Repeat Labs", ats_type="greenhouse")
    other = Company(name="Other Labs", ats_type="greenhouse")
    worker_session.add_all([company, other])
    await worker_session.flush()
    old = Posting(
        url=f"https://example.com/{uuid.uuid4()}",
        company_id=company.id,
        title="Backend Engineer",
        location="San Francisco, CA",
        description_raw="Build Zetastack tools.",
        first_seen_at=datetime.now(UTC) - timedelta(days=1),
    )
    new = Posting(
        url=f"https://example.com/{uuid.uuid4()}",
        company_id=other.id if change == "company" else company.id,
        title="Platform Engineer" if change == "title" else " BACKEND  Engineer ",
        location="San Jose, CA",
        description_raw="Build  Zetastack tools."
        + (" Different work." if change == "text" else ""),
    )
    worker_session.add_all([old, new])
    await worker_session.commit()
    found = await retrieve(worker_session, "Which jobs use Zetastack?")
    assert found.listing_total == 2
    assert found.job_total == (1 if change == "none" else 2)
    if change == "none":
        assert [p.posting_id for p in found.passages] == [new.id]
        assert found.passages[0].listing_count == 2
        assert set(found.passages[0].locations) == {old.location, new.location}
    else:
        assert {p.posting_id for p in found.passages} == {old.id, new.id}


@pytest.mark.parametrize(
    "question",
    ["Which jobs use Zetastack?", "Any backend engineer roles?", "Any jobs mentioning backend?"],
)
async def test_repeat_head_refills_and_no_repeat_in_further_list(worker_session, question):
    company = Company(name="Repeat Labs", ats_type="greenhouse")
    worker_session.add(company)
    await worker_session.flush()
    jobs = [
        Posting(
            url=f"https://example.com/{uuid.uuid4()}",
            company_id=company.id,
            title="Backend Engineer",
            location="San Francisco, CA",
            description_raw="Build Zetastack tools."
            if i < 8
            else f"Build Zetastack tools. Area {i}.",
        )
        for i in range(14)
    ]
    worker_session.add_all(jobs)
    await worker_session.commit()
    found = await retrieve(worker_session, question)
    assert found.job_total == 7 and found.listing_total == 14
    assert len(found.passages) == 5 and len(found.more) == 2
    assert sum(p.listing_count for p in (*found.passages, *found.more)) == 14
    assert len({p.posting_id for p in (*found.passages, *found.more)}) == 7


@pytest.mark.parametrize(
    "question", ["jobs that require Python", "Which jobs offer visa sponsorship?"]
)
async def test_computed_evidence_paths_collapse_too(worker_session, question):
    company = Company(name="Repeat Labs", ats_type="greenhouse")
    worker_session.add(company)
    await worker_session.flush()
    jobs = [
        Posting(
            url=f"https://example.com/{uuid.uuid4()}",
            company_id=company.id,
            title="Backend Engineer",
            location="San Francisco, CA",
            description_raw=(
                "Requirements\nPython experience. We offer visa sponsorship for this role."
            ),
        )
        for _ in range(6)
    ]
    worker_session.add_all(jobs)
    await worker_session.commit()
    found = await retrieve(worker_session, question)
    assert found.job_total == 1 and found.listing_total == 6
    assert len(found.passages) == 1 and found.passages[0].listing_count == 6
    assert not found.more


async def test_repeats_straddle_ranked_and_complete_further_list(worker_session):
    company = Company(name="Repeat Labs", ats_type="greenhouse")
    worker_session.add(company)
    await worker_session.flush()
    jobs = [
        Posting(
            url=f"https://example.com/{uuid.uuid4()}",
            company_id=company.id,
            title="Engineer",
            location="San Francisco, CA",
            description_raw="Zetastack tools." if i < 40 else f"Zetastack tools. Team {i}.",
        )
        for i in range(75)
    ]
    worker_session.add_all(jobs)
    await worker_session.commit()
    found = await retrieve(worker_session, "jobs using Zetastack")
    assert found.match_total == 75
    assert found.job_total == 36 and found.listing_total == 75
    assert len(found.passages) == 5 and len(found.more) == 31
    assert sum(p.listing_count for p in (*found.passages, *found.more)) == 75


async def test_promoted_representative_keeps_source_excerpt(worker_session):
    from packages.matching.repeats import collapse
    from packages.matching.retrieve import Listed, Passage, Retrieval

    company = Company(name="Repeat Labs", ats_type="greenhouse")
    worker_session.add(company)
    await worker_session.flush()
    old = Posting(
        url=f"https://example.com/{uuid.uuid4()}",
        company_id=company.id,
        title="Engineer",
        location="San Jose, CA",
        description_raw="Intro. Python is mandatory.",
        first_seen_at=datetime.now(UTC) - timedelta(days=1),
    )
    new = Posting(
        url=f"https://example.com/{uuid.uuid4()}",
        company_id=company.id,
        title="Engineer",
        location="San Francisco, CA",
        description_raw=old.description_raw,
    )
    worker_session.add_all([old, new])
    await worker_session.commit()
    found = Retrieval(
        attempted=True,
        passages=(
            Passage(
                "P1",
                old.id,
                old.title,
                company.name,
                old.location,
                old.url,
                "Python is mandatory.",
                None,
            ),
        ),
        more=(Listed(new.id, new.title, company.name, new.location, new.url),),
    )
    result = await collapse(worker_session, found, "jobs requiring Python", 5)
    assert result.passages[0].posting_id == new.id
    assert result.passages[0].excerpt == "Python is mandatory."


def test_selection_count_summary_reports_jobs_and_listings():
    from apps.api.chat_facts import render_postings
    from packages.matching.retrieve import Retrieval

    text = render_postings(
        Retrieval(attempted=True, job_total=2, listing_total=5), include_sources=False
    )
    assert "2 jobs (5 listings)" in text
