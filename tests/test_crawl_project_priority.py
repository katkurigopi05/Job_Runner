"""Companies whose open jobs need the owner's project technologies are crawled first.

On 2026-11-19, 270 of 4,544 companies held the 1,232 open jobs that directly
require a technology the owner's public projects declare. A crawl tick takes
every due company, so the order only decides which boards are fresh soonest:
those 270 first, then the rest by the schedule, as before. How often a board
is polled, the host floors and robots.txt are unchanged.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from packages.core.enums import SourceStatus
from packages.core.models import Candidate, Company, Posting, Project, QueueTask, User
from packages.core.queue import claim_task
from packages.crawler.crawl import CompanyResult
from packages.crawler.dispatch import CRAWL_COMPANY_TASK_KIND, dispatch
from packages.crawler.runs import due_companies, record_attempt, start_run
from packages.matching import skill_links

HOUR = 3600


async def _company(session, name: str) -> Company:
    company = Company(
        name=name,
        slug=name.lower(),
        ats_type="greenhouse",
        poll_interval_s=HOUR,
        source_status=SourceStatus.VERIFIED.value,
    )
    session.add(company)
    await session.flush()
    return company


async def _python_project(session) -> None:
    user = User(email=f"{uuid.uuid4().hex}@example.test")
    session.add(user)
    await session.flush()
    candidate = Candidate(user_id=user.id, name="Owner", email="owner@example.test")
    session.add(candidate)
    await session.flush()
    session.add(
        Project(
            candidate_id=candidate.id,
            source="github",
            external_id=uuid.uuid4().hex,
            name="service",
            url="https://github.com/owner/service",
            language="Python",
        )
    )
    await session.flush()
    await skill_links.link_projects(session, candidate.id)


async def _job(session, company: Company, requirement: str, *, link: bool = True) -> None:
    posting = Posting(
        company_id=company.id,
        url=f"https://jobs.example.test/{uuid.uuid4().hex}",
        title="Engineer",
        description_raw=f"Requirements\n{requirement} is required.",
        content_hash=uuid.uuid4().hex,
    )
    session.add(posting)
    await session.flush()
    if link:
        await skill_links.link_postings(session, [posting.id])


async def _two_due(session) -> tuple[Company, Company, datetime]:
    """`Early` is owed sooner by the schedule; only `Match` needs a project skill."""
    early = await _company(session, "Early")
    match = await _company(session, "Match")
    now = datetime.now(UTC)
    await record_attempt(
        session, early, CompanyResult(company="Early"), now=now - timedelta(hours=3)
    )
    await record_attempt(
        session, match, CompanyResult(company="Match"), now=now - timedelta(hours=2)
    )
    return early, match, now


async def test_a_company_whose_jobs_need_project_skills_is_due_first(db_session) -> None:
    early, match, now = await _two_due(db_session)
    await _python_project(db_session)
    await _job(db_session, match, "Python")
    await _job(db_session, early, "Java")

    due = await due_companies(db_session, limit=10, now=now + timedelta(hours=2))

    assert [company.name for company in due[:2]] == ["Match", "Early"]


async def test_without_saved_links_the_schedule_alone_decides(db_session) -> None:
    early, match, now = await _two_due(db_session)
    await _python_project(db_session)
    await _job(db_session, match, "Python", link=False)

    due = await due_companies(db_session, limit=10, now=now + timedelta(hours=2))

    assert [company.name for company in due[:2]] == ["Early", "Match"]


async def test_dispatch_hands_the_tasks_out_in_that_order(db_session) -> None:
    companies = [await _company(db_session, name) for name in ("First", "Second", "Third")]
    run = await start_run(db_session, trigger="manual", companies_total=3)

    await dispatch(db_session, companies, run)

    tasks = (
        await db_session.scalars(
            select(QueueTask)
            .where(QueueTask.kind == CRAWL_COMPANY_TASK_KIND)
            .order_by(QueueTask.run_after)
        )
    ).all()
    assert [task.payload_json["company_id"] for task in tasks] == [str(c.id) for c in companies]
    assert len({task.run_after for task in tasks}) == 3
    claimed = []
    for _ in companies:
        got = await claim_task(db_session, worker_id="w", kinds=[CRAWL_COMPANY_TASK_KIND])
        assert got is not None
        claimed.append(got.task.payload_json["company_id"])
    assert claimed == [str(c.id) for c in companies]
