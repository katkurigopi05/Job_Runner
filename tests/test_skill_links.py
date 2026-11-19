"""The saved skill graph: jobs, projects and base résumés linked to vocabulary skills.

Each link must say what the in-memory readers already say. A posting's links are
`fresh_requirements` read through `skill_evidence`; a project's are its primary
language and dependency declarations; a résumé's are `resume_skills` over its
lines. A link written from text that has since changed, or by an older
extractor, is stale and is read again.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select, update

from packages.core.models import Candidate, Posting, Project, Resume, User
from packages.core.models_skill_links import (
    PostingSkill,
    PostingSkillRead,
    ProjectSkill,
    ResumeSkill,
)
from packages.matching import skill_links
from packages.matching.requirements import EXTRACTOR_VERSION

JOB = """About the role
We build with Kafka.

Requirements
- 5+ years of experience with Python
- Experience with PostgreSQL
- Experience with Go or Rust

Nice to have
- Kubernetes
"""


async def _candidate(session) -> Candidate:
    user = User(email=f"{uuid.uuid4().hex}@example.com")
    session.add(user)
    await session.flush()
    candidate = Candidate(user_id=user.id, name="Owner", email="owner@example.com")
    session.add(candidate)
    await session.flush()
    return candidate


async def _posting(session, description: str | None = JOB, **fields) -> Posting:
    posting = Posting(
        url=f"https://example.com/{uuid.uuid4().hex}",
        title=fields.pop("title", "Backend Engineer"),
        description_raw=description,
        content_hash=fields.pop("content_hash", uuid.uuid4().hex),
        **fields,
    )
    session.add(posting)
    await session.flush()
    return posting


async def _links(session, posting: Posting) -> dict[str, PostingSkill]:
    rows = await session.scalars(select(PostingSkill).where(PostingSkill.posting_id == posting.id))
    return {row.skill: row for row in rows}


async def _read(session, posting: Posting) -> PostingSkillRead | None:
    return await session.get(PostingSkillRead, posting.id, populate_existing=True)


def _project(candidate: Candidate, **fields) -> Project:
    return Project(
        candidate_id=candidate.id,
        external_id=uuid.uuid4().hex,
        name=fields.pop("name", "service"),
        url=f"https://github.com/owner/{uuid.uuid4().hex}",
        **fields,
    )


# --- postings ---------------------------------------------------------------


async def test_a_posting_links_each_skill_once_at_its_strongest_reading(db_session) -> None:
    posting = await _posting(db_session)

    linked = await skill_links.link_postings(db_session, [posting.id])

    assert linked == 1
    links = await _links(db_session, posting)
    assert {key: (row.strength, row.is_option) for key, row in links.items()} == {
        "python": ("required", False),
        "postgresql": ("required", False),
        "rust": ("required", True),
        "kubernetes": ("preferred", False),
        "kafka": ("named", False),
    }
    assert links["python"].quote == "- 5+ years of experience with Python"
    read = await _read(db_session, posting)
    assert read is not None
    assert read.readable and read.extractor_version == EXTRACTOR_VERSION
    assert read.content_hash == posting.content_hash


async def test_links_are_what_the_in_memory_readers_say(db_session) -> None:
    """A stale stored reading is re-read in memory, as `fresh_requirements` does."""
    from packages.matching.evidence import skill_evidence
    from packages.matching.required_skills import fresh_requirements

    posting = await _posting(
        db_session, requirements_json={"skills": {"required": []}}, requirements_version=1
    )
    await skill_links.link_postings(db_session, [posting.id])

    fresh = fresh_requirements(
        posting.requirements_json, posting.requirements_version, posting.description_raw
    )
    for key, row in (await _links(db_session, posting)).items():
        evidence = skill_evidence(fresh, key)
        assert evidence is not None
        assert (row.strength, row.is_option, row.quote) == (
            evidence.strength.value,
            evidence.option,
            evidence.quote,
        )
    assert "python" in await _links(db_session, posting)


async def test_a_posting_without_text_is_recorded_as_unreadable_not_as_asking_nothing(
    db_session,
) -> None:
    posting = await _posting(db_session, description=None)

    await skill_links.link_postings(db_session, [posting.id])

    assert await _links(db_session, posting) == {}
    read = await _read(db_session, posting)
    assert read is not None and read.readable is False


async def test_relinking_replaces_what_the_old_text_said(db_session) -> None:
    posting = await _posting(db_session)
    await skill_links.link_postings(db_session, [posting.id])

    await db_session.execute(
        update(Posting)
        .where(Posting.id == posting.id)
        .values(
            description_raw="Requirements\n- Experience with Java\n",
            content_hash="changed",
            requirements_json=None,
            requirements_version=None,
        )
    )
    await skill_links.link_postings(db_session, [posting.id])

    assert set(await _links(db_session, posting)) == {"java"}
    read = await _read(db_session, posting)
    assert read is not None and read.content_hash == "changed"


async def test_refresh_reads_only_what_is_missing_or_stale(db_session, monkeypatch) -> None:
    fresh = await _posting(db_session)
    edited = await _posting(db_session)
    await skill_links.link_postings(db_session, [fresh.id, edited.id])
    never = await _posting(db_session)
    await db_session.execute(
        update(Posting).where(Posting.id == edited.id).values(content_hash="edited")
    )

    report = await skill_links.refresh_postings(db_session)

    assert report.linked == 2
    assert report.skipped == 0
    assert (await _read(db_session, edited)).content_hash == "edited"  # type: ignore[union-attr]
    assert (await _read(db_session, never)) is not None
    assert (await skill_links.refresh_postings(db_session)).linked == 0

    # A new extractor makes every reading stale.
    monkeypatch.setattr(skill_links, "EXTRACTOR_VERSION", EXTRACTOR_VERSION + 1)
    assert (await skill_links.refresh_postings(db_session)).linked == 3


async def test_a_guarded_write_skips_text_that_changed_after_it_was_read(db_session) -> None:
    """A crawl that edits a posting mid-refresh has written the newer links already."""
    posting = await _posting(db_session)
    readings = await skill_links.read_postings(db_session, [posting.id])
    await db_session.execute(
        update(Posting).where(Posting.id == posting.id).values(content_hash="crawl")
    )

    report = await skill_links.write_links(db_session, readings, guarded=True)

    assert report.linked == 0 and report.skipped == 1
    assert await _read(db_session, posting) is None


async def test_the_crawler_links_what_it_stores(db_session) -> None:
    from packages.core.models import Company
    from packages.crawler.crawl import _store
    from packages.crawler.extract import ExtractedPosting

    company = Company(name=f"Acme {uuid.uuid4().hex[:6]}")
    db_session.add(company)
    await db_session.flush()

    def listing(description: str, content_hash: str) -> ExtractedPosting:
        return ExtractedPosting(
            ats_type="greenhouse",
            external_id="1",
            url="https://boards.greenhouse.io/acme/jobs/1",
            title="Backend Engineer",
            location="San Francisco, CA",
            description_raw=description,
            content_hash=content_hash,
        )

    await _store(db_session, company, [listing(JOB, "first")], us_only=False, max_age_days=0)
    posting = await db_session.scalar(select(Posting).where(Posting.company_id == company.id))
    assert posting is not None
    assert "python" in await _links(db_session, posting)

    edited = listing("Requirements\n- Experience with Java\n", "second")
    await _store(db_session, company, [edited], us_only=False, max_age_days=0)
    assert set(await _links(db_session, posting)) == {"java"}
    read = await _read(db_session, posting)
    assert read is not None and read.content_hash == "second"


async def test_a_listing_the_crawler_stored_without_text_is_unreadable(db_session) -> None:
    """The crawler stores an empty reading for it, which is not "asks for nothing"."""
    from packages.core.models import Company
    from packages.crawler.crawl import _store
    from packages.crawler.extract import ExtractedPosting

    company = Company(name=f"Workday {uuid.uuid4().hex[:6]}")
    db_session.add(company)
    await db_session.flush()
    listing = ExtractedPosting(
        ats_type="workday",
        external_id="R1",
        url="https://acme.wd1.myworkdayjobs.com/jobs/job/R1",
        title="Data Engineer",
        location="San Jose, CA",
        content_hash="listed",
    )

    await _store(db_session, company, [listing], us_only=False, max_age_days=0)

    posting = await db_session.scalar(select(Posting).where(Posting.company_id == company.id))
    assert posting is not None and posting.requirements_json is not None
    read = await _read(db_session, posting)
    assert read is not None and read.readable is False


async def test_a_refresh_skips_a_posting_a_crawl_is_holding(committing_sessionmaker) -> None:
    async with committing_sessionmaker() as setup:
        posting = await _posting(setup)
        await setup.commit()

    async with committing_sessionmaker() as crawl, committing_sessionmaker() as refresh:
        await crawl.execute(select(Posting.id).where(Posting.id == posting.id).with_for_update())
        report = await skill_links.refresh_postings(refresh)
        await refresh.rollback()
        await crawl.rollback()
    assert report.linked == 0 and report.skipped == 1

    async with committing_sessionmaker() as later:
        assert (await skill_links.refresh_postings(later)).linked == 1


# --- projects ----------------------------------------------------------------


async def test_a_project_links_its_language_and_declared_dependencies(db_session) -> None:
    candidate = await _candidate(db_session)
    service = _project(
        candidate, language="Python", dependency_technologies=["FastAPI", "PostgreSQL"]
    )
    fork = _project(candidate, language="Go", is_fork=True)
    described = _project(candidate, description="A Kubernetes operator", topics_json=["rust"])
    db_session.add_all([service, fork, described])
    await db_session.flush()

    await skill_links.link_projects(db_session, candidate.id)

    rows = (
        await db_session.scalars(
            select(ProjectSkill).where(
                ProjectSkill.project_id.in_([service.id, fork.id, described.id])
            )
        )
    ).all()
    assert {(row.project_id, row.skill, row.origin, row.declared_as) for row in rows} == {
        (service.id, "python", "language", "Python"),
        (service.id, "fastapi", "dependency", "FastAPI"),
        (service.id, "postgresql", "dependency", "PostgreSQL"),
    }


async def test_relinking_projects_drops_a_dependency_the_repository_no_longer_declares(
    db_session,
) -> None:
    candidate = await _candidate(db_session)
    service = _project(candidate, language="Python", dependency_technologies=["Redis"])
    db_session.add(service)
    await db_session.flush()
    await skill_links.link_projects(db_session, candidate.id)

    service.dependency_technologies = []
    await db_session.flush()
    await skill_links.link_projects(db_session, candidate.id)

    skills = await db_session.scalars(
        select(ProjectSkill.skill).where(ProjectSkill.project_id == service.id)
    )
    assert set(skills) == {"python"}


async def test_github_sync_links_the_projects_it_imports(db_session) -> None:
    from packages.github.client import GitHubClient
    from packages.github.sync import sync_from_github

    candidate = await _candidate(db_session)
    repository = {
        "id": 11,
        "name": "harness",
        "full_name": "owner/harness",
        "html_url": "https://github.com/owner/harness",
        "language": "TypeScript",
        "topics": [],
        "private": False,
        "fork": False,
        "pushed_at": "2026-11-01T10:00:00Z",
        "default_branch": "main",
        "owner": {"login": "owner"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "/git/trees/" in request.url.path:
            return httpx.Response(200, json={"tree": []})
        return httpx.Response(200, json=[repository])

    client = GitHubClient(transport=httpx.MockTransport(handler))
    await sync_from_github(db_session, candidate.id, "owner", client=client)

    skills = await db_session.scalars(
        select(ProjectSkill.skill)
        .join(Project, Project.id == ProjectSkill.project_id)
        .where(Project.candidate_id == candidate.id)
    )
    assert set(skills) == {"typescript"}


# --- résumés -----------------------------------------------------------------


def _resume(candidate: Candidate, lines: list[str], **fields) -> Resume:
    return Resume(
        candidate_id=candidate.id,
        version=fields.pop("version", 1),
        storage_ref=f"resumes/{uuid.uuid4().hex}.txt",
        parsed_json={"raw_lines": lines},
        **fields,
    )


async def test_a_base_resume_links_the_skills_it_lists(db_session) -> None:
    candidate = await _candidate(db_session)
    base = _resume(candidate, ["Skills", "Python, PostgreSQL"])
    db_session.add(base)
    await db_session.flush()

    assert await skill_links.link_resume(db_session, base) == 2

    skills = await db_session.scalars(
        select(ResumeSkill.skill).where(ResumeSkill.resume_id == base.id)
    )
    assert set(skills) == {"python", "postgresql"}


async def test_a_tailored_resume_is_not_linked(db_session) -> None:
    candidate = await _candidate(db_session)
    posting = await _posting(db_session)
    tailored = _resume(candidate, ["Python"], version=2, tailored_for_posting_id=posting.id)
    db_session.add(tailored)
    await db_session.flush()

    assert await skill_links.link_resume(db_session, tailored) == 0
    assert (
        await db_session.scalar(select(ResumeSkill).where(ResumeSkill.resume_id == tailored.id))
        is None
    )


async def test_uploading_a_resume_links_it(
    client, complete_candidate, committing_sessionmaker
) -> None:
    """`complete_candidate` uploads the suite's sample résumé through the API."""
    async with committing_sessionmaker() as session:
        resume_ids = (
            await session.scalars(
                select(Resume.id).where(
                    Resume.candidate_id == uuid.UUID(complete_candidate["candidate_id"])
                )
            )
        ).all()
        skills = await session.scalars(
            select(ResumeSkill.skill).where(ResumeSkill.resume_id.in_(resume_ids))
        )
        assert {"python", "postgresql"} <= set(skills)


# --- reading the graph -------------------------------------------------------


async def test_jobs_are_matched_to_projects_on_direct_requirements_only(db_session) -> None:
    candidate = await _candidate(db_session)
    db_session.add_all(
        [
            _project(candidate, language="Python", dependency_technologies=["PostgreSQL"]),
            _project(candidate, language="Rust"),
            _project(candidate, language="Kotlin", is_private=True),
            _project(candidate, language="Java", include=False),
        ]
    )
    await db_session.flush()
    both = await _posting(db_session, title="Both")
    one = await _posting(
        db_session, "Requirements\n- Experience with Python\n- Kotlin\n", title="One"
    )
    excluded = await _posting(db_session, "Requirements\n- Experience with Java\n", title="Java")
    closed = await _posting(db_session, title="Closed", closed_at=datetime.now(UTC))
    await skill_links.link_postings(db_session, [both.id, one.id, excluded.id, closed.id])
    await skill_links.link_projects(db_session, candidate.id)

    overlap = await skill_links.project_job_overlap(db_session, candidate.id, limit=10)

    assert [(row.title, row.skills) for row in overlap.jobs] == [
        # Rust is only one choice of two here, and Kubernetes only preferred.
        ("Both", ("postgresql", "python")),
        ("One", ("python",)),
    ]
    assert overlap.total == 2


async def test_coverage_says_how_much_of_the_open_corpus_is_linked(db_session) -> None:
    linked = await _posting(db_session)
    await _posting(db_session)
    unreadable = await _posting(db_session, description=None)
    await skill_links.link_postings(db_session, [linked.id, unreadable.id])

    coverage = await skill_links.coverage(db_session)

    assert coverage.linked >= 2
    assert coverage.unreadable >= 1
    assert coverage.stale >= 1
    assert coverage.open == coverage.linked + coverage.stale


@pytest.mark.parametrize("held_count", [1, 2])
async def test_new_jobs_are_linked_when_a_board_replaces_or_reduces_its_list(
    db_session, held_count
) -> None:
    from packages.core.models import Company
    from packages.crawler.crawl import _store
    from packages.crawler.extract import ExtractedPosting

    company = Company(name="Changing board")
    db_session.add(company)
    await db_session.flush()

    def listing(external_id: str) -> ExtractedPosting:
        return ExtractedPosting(
            ats_type="greenhouse",
            external_id=external_id,
            url=f"https://boards.greenhouse.io/acme/jobs/{external_id}",
            title="Backend Engineer",
            location="San Francisco, CA",
            description_raw=JOB,
            content_hash=f"hash-{external_id}",
        )

    await _store(
        db_session,
        company,
        [listing(f"old-{i}") for i in range(held_count)],
        us_only=False,
        max_age_days=0,
    )
    result = await _store(db_session, company, [listing("new")], us_only=False, max_age_days=0)
    posting = await db_session.scalar(
        select(Posting).where(Posting.company_id == company.id, Posting.external_id == "new")
    )
    assert posting is not None
    assert result.new == [posting.id]
    assert "python" in await _links(db_session, posting)
    assert await _read(db_session, posting) is not None
