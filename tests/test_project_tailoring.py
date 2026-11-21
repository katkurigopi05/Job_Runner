"""Job-first project ranking, source-scoped prose and saved document parity."""

from __future__ import annotations

import io
import uuid
from datetime import timedelta

import pytest
from docx import Document

from packages.core.models import Project
from packages.github.select import matched_requirements, score, select_projects
from packages.llm.provider import StubProvider
from packages.matching.skill_links import link_projects
from packages.tailor.project_rewrite import rewrite_project, validate_description
from packages.tailor.studio import RoleTrack, pick_projects, synthesize
from packages.tailor.studio_sources import build_source
from tests.test_projects import NOW, make_project
from tests.test_resume_studio import base_resume

JOB = "Requirements:\nPython, PostgreSQL and Docker.\nNice to have:\nReact."
ORIGINAL = "Built a Python service with PostgreSQL for job matching."
TAILORED = "Built a job matching service with Python and PostgreSQL."


class Writer(StubProvider):
    def __init__(self, answer=TAILORED):
        self.answer = answer
        self.calls = []

    async def complete(self, system, user, **kwargs):
        self.calls.append(user)
        return self.answer


@pytest.mark.parametrize("include", [None, True, False])
@pytest.mark.parametrize("pinned", [False, True])
async def test_forks_are_never_ranked_selected_or_rewritten(include, pinned):
    fork = make_project(is_fork=True, include=include, pinned=pinned, description=ORIGINAL)
    assert select_projects([fork], JOB) == []
    assert (
        pick_projects(
            [fork],
            base_resume(),
            JOB,
            RoleTrack.SWE,
            selected_ids=[str(fork.id)],
            replace_source_projects=True,
        )
        == []
    )
    provider = Writer()
    result = await rewrite_project(provider, fork, JOB)
    assert result.status == "excluded fork" and not provider.calls
    assert validate_description(fork, ORIGINAL, ORIGINAL)


async def test_studio_save_rejects_an_explicitly_included_pinned_fork(
    client,
    complete_candidate,
    committing_sessionmaker,
):
    candidate_id = complete_candidate["candidate_id"]
    async with committing_sessionmaker() as session:
        fork = make_project(
            candidate_id=uuid.UUID(candidate_id),
            is_fork=True,
            include=True,
            pinned=True,
        )
        session.add(fork)
        await session.commit()
        project_id = str(fork.id)
    preview = (
        await client.post(
            "/resumes/synthesize",
            json={
                "candidate_id": candidate_id,
                "job_description": JOB,
                "selected_project_ids": [project_id],
            },
        )
    ).json()
    assert not preview["projects"]
    response = await client.post(
        "/resumes/synthesize/save",
        json={
            "candidate_id": candidate_id,
            "base_resume_id": preview["base_resume_id"],
            "contact": preview["contact"],
            "sections": preview["sections"],
            "selected_project_ids": [project_id],
        },
    )
    assert response.status_code == 400
    assert "Forked projects" in response.json()["error"]["message"]


def test_required_coverage_beats_recency_popularity_and_keyword_stuffing():
    older = make_project(
        name="older",
        description=ORIGINAL,
        language="Python",
        dependency_technologies=["PostgreSQL", "Docker"],
        pushed_at=NOW - timedelta(days=2000),
        stars=0,
        topics_json=[],
    )
    recent = make_project(
        name="Python PostgreSQL Docker",
        language="Python",
        description=JOB,
        topics_json=["PostgreSQL", "Docker"],
        stars=500,
        pushed_at=NOW,
    )
    assert score(older, JOB, now=NOW) > score(recent, JOB, now=NOW)
    assert select_projects([recent, older], JOB, now=NOW)[0] is older
    assert set(matched_requirements(older, JOB)) == {"Python", "PostgreSQL", "Docker"}
    assert matched_requirements(recent, JOB) == ["Python"]


def test_alternatives_and_preferences_do_not_become_direct_requirements():
    repo = make_project(language="Python", dependency_technologies=["React"])
    assert matched_requirements(repo, "Requirements:\nPython or Go.\nPreferred:\nReact.") == []


def test_zero_requirement_overlap_is_not_auto_selected_but_pins_are_preserved():
    repo = make_project(language="Rust", description="A Rust service.")
    picks = pick_projects([repo], base_resume(), JOB, RoleTrack.SWE)
    assert not picks[0].chosen
    repo.pinned = True
    assert pick_projects([repo], base_resume(), JOB, RoleTrack.SWE)[0].chosen


async def test_description_rewrite_is_transient_and_is_the_printed_text():
    repo = make_project(
        description=ORIGINAL, language="Python", dependency_technologies=["PostgreSQL"]
    )
    base = base_resume()
    provider = Writer()
    draft = await synthesize(
        base, build_source(base), JOB, RoleTrack.SWE, projects=[repo], provider=provider
    )
    assert len(provider.calls) == 1
    assert draft.project_rewrites[0].status == "rewritten"
    assert TAILORED in draft.html and ORIGINAL not in draft.html
    assert repo.description == ORIGINAL
    assert draft.calls == 1


async def test_ranked_projects_replace_source_section_without_duplicates():
    base = base_resume()
    base.sections["projects"] = [
        "jobrunner [GitHub]",
        "Python, PostgreSQL",
        "Built a Python service with PostgreSQL.",
        "Other project [GitHub]",
        "Rust",
        "Built a Rust service.",
    ]
    repo = make_project(description=ORIGINAL, dependency_technologies=["PostgreSQL"])
    draft = await synthesize(
        base,
        build_source(base),
        JOB,
        RoleTrack.SWE,
        projects=[repo],
        replace_source_projects=True,
    )
    assert draft.picks[0].chosen
    assert "projects" not in draft.resume.sections
    assert "Other project" not in draft.html
    assert draft.html.count('class="project-name"') == 1
    assert "Other project [GitHub]" in base.sections["projects"]


@pytest.mark.parametrize(
    "answer",
    [
        "Built a Python service with Kubernetes for job matching.",
        "Built a Python service with PostgreSQL for job matching, serving 500 users.",
    ],
)
async def test_invented_technology_or_metrics_keep_the_original(answer):
    repo = make_project(description=ORIGINAL, language="Python")
    rewrite = await rewrite_project(Writer(answer), repo, JOB)
    assert rewrite.status == "refused" and rewrite.tailored == ORIGINAL


async def test_no_source_description_means_no_model_call_or_invented_summary():
    repo = make_project(description=None)
    provider = Writer()
    rewrite = await rewrite_project(provider, repo, JOB)
    assert rewrite.status == "no source description" and not provider.calls
    assert validate_description(repo, "", "Built a Python service.")


def test_save_rejects_changed_source_and_borrowed_project_claims():
    repo = make_project(description=ORIGINAL)
    assert validate_description(repo, "An older source", TAILORED)
    assert validate_description(repo, ORIGINAL, "Built a Kubernetes service for job matching.")
    assert validate_description(repo, ORIGINAL, TAILORED) is None


async def test_studio_preview_save_and_docx_retain_approved_prose(
    client,
    complete_candidate,
    committing_sessionmaker,
    monkeypatch,
):
    from apps.api.routers import resume_studio

    candidate_id = complete_candidate["candidate_id"]
    async with committing_sessionmaker() as session:
        repo = make_project(candidate_id=uuid.UUID(candidate_id), description=ORIGINAL)
        session.add(repo)
        await session.flush()
        await link_projects(session, uuid.UUID(candidate_id))
        await session.commit()
        project_id = str(repo.id)
    writer = Writer()
    monkeypatch.setattr(resume_studio.llm_router, "build_provider", lambda name: writer)
    response = await client.post(
        "/resumes/synthesize",
        json={
            "candidate_id": candidate_id,
            "job_description": JOB,
            "use_model": True,
            "replace_source_projects": True,
            "model_provider": "kaggle",
        },
    )
    assert response.status_code == 200, response.text
    preview = response.json()
    item = next(p for p in preview["projects"] if p["id"] == project_id)
    assert item["tailored_description"] == TAILORED
    body = {
        "candidate_id": candidate_id,
        "base_resume_id": preview["base_resume_id"],
        "selected_project_ids": [project_id],
        "contact": preview["contact"],
        "sections": preview["sections"],
        "project_rewrites": [
            {"project_id": project_id, "original": ORIGINAL, "tailored": TAILORED}
        ],
    }
    forged = {
        **body,
        "project_rewrites": [
            {
                "project_id": project_id,
                "original": ORIGINAL,
                "tailored": "Built a Python service with Kubernetes for job matching.",
            }
        ],
    }
    assert (await client.post("/resumes/synthesize/save", json=forged)).status_code == 400
    response = await client.post("/resumes/synthesize/save", json=body)
    assert response.status_code == 201, response.text
    resume_id = response.json()["resume"]["id"]
    # A later repository edit must not change an already saved version's DOCX.
    async with committing_sessionmaker() as session:
        repo = await session.get(Project, uuid.UUID(project_id))
        assert repo.description == ORIGINAL
        repo.description = "A newer description that was not approved."
        await session.commit()
    exported = await client.get(f"/resumes/{resume_id}/docx")
    words = "\n".join(p.text for p in Document(io.BytesIO(exported.content)).paragraphs)
    assert TAILORED in words and "newer description" not in words


async def test_studio_live_mcp_uses_current_description_and_declarations(
    worker_session,
    monkeypatch,
):
    import httpx
    from sqlalchemy import select

    from apps.api.studio_projects import refresh_projects
    from packages.github.client import GitHubClient
    from tests.test_github_live import REPO, SHA, handler_factory
    from tests.test_graph_rag import seed

    await seed(worker_session)
    repo = await worker_session.scalar(select(Project))
    repo.full_name, repo.external_id = "owner/example", "17"
    repo.description, repo.language = "Obsolete description", "Rust"
    repo.dependency_technologies = ["Kubernetes"]
    await worker_session.commit()
    handler, requests = handler_factory(repo={**REPO, "description": ORIGINAL})
    original_init = GitHubClient.__init__

    def patched(self, token=None, **kwargs):
        original_init(self, token, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(GitHubClient, "__init__", patched)
    fresh, notice = await refresh_projects([repo])
    assert len(fresh) == 1 and len(requests) == 4
    assert fresh[0].description == ORIGINAL
    assert fresh[0].language == "Python"
    assert fresh[0].dependency_technologies == ["FastAPI"]
    assert SHA in fresh[0].url
    assert repo.description == "Obsolete description" and not worker_session.dirty
    assert "1 of 1" in notice

    failed_handler, _ = handler_factory(repo={**REPO, "private": True})

    def failing(self, token=None, **kwargs):
        original_init(self, token, transport=httpx.MockTransport(failed_handler))

    monkeypatch.setattr(GitHubClient, "__init__", failing)
    fresh, notice = await refresh_projects([repo])
    assert not fresh and "1 failed" in notice

    from packages.llm.provider import LLMError

    def rate_limited(request):
        return httpx.Response(403, headers={"X-RateLimit-Remaining": "0"})

    def exhausted(self, token=None, **kwargs):
        original_init(self, token, transport=httpx.MockTransport(rate_limited))

    monkeypatch.setattr(GitHubClient, "__init__", exhausted)
    with pytest.raises(LLMError, match="GitHub rate limit reached"):
        await refresh_projects([repo])


async def test_a_private_project_is_printed_as_written_and_never_sent_to_a_model():
    repo = make_project(description=ORIGINAL, is_private=True, include=True)
    provider = Writer()

    rewrite = await rewrite_project(provider, repo, JOB)

    assert rewrite.status == "private project" and rewrite.tailored == ORIGINAL
    assert not provider.calls


async def test_replacing_with_nothing_keeps_the_source_projects_section():
    base = base_resume()
    base.sections["projects"] = ["Other project [GitHub]", "Rust", "Built a Rust service."]
    unrelated = make_project(description="A Rust game.", language="Rust")

    draft = await synthesize(
        base,
        build_source(base),
        JOB,
        RoleTrack.SWE,
        projects=[unrelated],
        replace_source_projects=True,
    )

    assert not any(pick.chosen for pick in draft.picks)
    assert draft.resume.sections["projects"] == base.sections["projects"]
