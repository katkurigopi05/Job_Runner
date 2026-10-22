"""A named employer's applications must not inherit the global status counts."""

from datetime import UTC, datetime

import pytest

from packages.core.models import Application, Company, Posting
from tests.test_chat_skill_gaps import _ask, _owner


def test_prompt_keeps_company_status_separate_from_global_counts():
    from apps.api.routers.chat import SYSTEM

    assert "YOUR APPLICATIONS AT" in SYSTEM
    assert "global counts" in SYSTEM


async def _setup(session, *, linked=True, count=1):
    profile, _ = await _owner(session)
    company = Company(name="Cloudflare", ats_type="greenhouse", slug="cloudflare")
    other = Company(name="Other", ats_type="greenhouse", slug="other")
    session.add_all([company, other])
    await session.flush()
    posting = Posting(
        company_id=company.id,
        title="Systems Engineer",
        url="https://boards.greenhouse.io/cloudflare/jobs/8042034",
    )
    session.add(posting)
    await session.flush()
    for index in range(count):
        session.add(
            Application(
                candidate_id=profile.candidate_id,
                profile_id=profile.id,
                posting_id=posting.id if linked else None,
                url=f"https://boards.greenhouse.io/cloudflare/jobs/{8042034 + index}",
                status="needs_review" if index == 0 else "failed",
                failure_reason=None if index == 0 else "job_closed",
                created_at=datetime(2026, 8, 26, tzinfo=UTC),
                updated_at=datetime(2026, 8, 27, tzinfo=UTC),
            )
        )
    session.add(
        Application(
            candidate_id=profile.candidate_id,
            profile_id=profile.id,
            url="https://boards.greenhouse.io/other/jobs/2",
            status="submitted",
        )
    )
    await session.commit()


@pytest.mark.parametrize("count", [1, 3])
async def test_named_company_applications_are_listed(client, worker_session, monkeypatch, count):
    await _setup(worker_session, count=count)
    _, seen = await _ask(client, monkeypatch, "What's the status of my Cloudflare application?")
    section = seen["user"].split("YOUR APPLICATIONS AT Cloudflare:")[1].split("POSTINGS:")[0]
    assert f"{count} application" in section
    assert "Systems Engineer" in section
    assert "status=needs_review" in section
    assert "failure_reason=none" in section
    assert "created=2026-08-26" in section and "updated=2026-08-27" in section
    assert "/other/" not in section and "submitted" not in section
    if count > 1:
        assert "status=failed" in section and "failure_reason=job_closed" in section


async def test_no_applications_is_explicit(client, worker_session, monkeypatch):
    await _setup(worker_session, count=0)
    _, seen = await _ask(client, monkeypatch, "What's the status of my Cloudflare application?")
    assert "no applications at Cloudflare" in seen["user"]


async def test_orphan_application_matches_board_url(client, worker_session, monkeypatch):
    await _setup(worker_session, linked=False)
    _, seen = await _ask(client, monkeypatch, "What's the status of my Cloudflare application?")
    section = seen["user"].split("YOUR APPLICATIONS AT Cloudflare:")[1].split("POSTINGS:")[0]
    assert "https://boards.greenhouse.io/cloudflare/jobs/8042034" in section
    assert "status=needs_review" in section


async def test_company_jobs_question_gets_no_application_section(
    client, worker_session, monkeypatch
):
    await _setup(worker_session)
    _, seen = await _ask(client, monkeypatch, "jobs at Cloudflare")
    assert "YOUR APPLICATIONS AT" not in seen["user"]


@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.lever.co/cloudflare/123",
        "https://boards.greenhouse.io/cloudflare-other/jobs/123",
        "https://boards.greenhouse.io.evil.test/cloudflare/jobs/123",
    ],
)
async def test_orphan_fallback_requires_exact_vendor_and_board(
    client, worker_session, monkeypatch, url
):
    from sqlalchemy import select

    await _setup(worker_session, linked=False)
    application = await worker_session.scalar(
        select(Application).where(Application.status == "needs_review")
    )
    application.url = url
    await worker_session.commit()
    _, seen = await _ask(client, monkeypatch, "What's the status of my Cloudflare application?")
    assert "no applications at Cloudflare" in seen["user"]


async def test_linked_posting_takes_precedence_over_the_url(client, worker_session, monkeypatch):
    from sqlalchemy import select

    await _setup(worker_session)
    other = await worker_session.scalar(select(Company).where(Company.name == "Other"))
    other_posting = Posting(company_id=other.id, title="Other role", url="https://x.test/other")
    worker_session.add(other_posting)
    await worker_session.flush()
    application = await worker_session.scalar(
        select(Application).where(Application.status == "needs_review")
    )
    application.posting_id = other_posting.id
    await worker_session.commit()
    _, seen = await _ask(client, monkeypatch, "What's the status of my Cloudflare application?")
    assert "no applications at Cloudflare" in seen["user"]
