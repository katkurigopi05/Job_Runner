"""Graph paths must preserve source scope, demand semantics and prompt privacy."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from packages.core.models import Candidate, Company, Match, Posting, Profile, Project, Resume, User
from packages.matching.graph_rag import (
    Entity,
    build_graph,
    graph_intent,
    graph_result,
    retrieve_graph,
)
from packages.matching.requirements import EXTRACTOR_VERSION, extract

OWNER = uuid.uuid4()


def project(**changes):
    values = dict(
        id=uuid.uuid4(),
        candidate_id=OWNER,
        source="github",
        external_id=uuid.uuid4().hex,
        name="source-project",
        url="https://github.com/example/source-project",
        language="Python",
        dependency_technologies=["Postgres"],
        is_private=False,
        is_fork=False,
        include=None,
    )
    return Project(**(values | changes))


def resume(text="Built Python services in a distinctive private résumé sentence", **changes):
    values = dict(
        id=uuid.uuid4(),
        candidate_id=OWNER,
        storage_ref="private.pdf",
        version=1,
        parsed_json={"raw_lines": [text]},
        tailored_for_posting_id=None,
    )
    return Resume(**(values | changes))


def posting(body="Requirements\nPython and PostgreSQL are required.", **changes):
    values = dict(
        id=uuid.uuid4(),
        title="Backend Engineer",
        location="Remote, US",
        url=f"https://jobs.example.test/{uuid.uuid4().hex}",
        description_raw=body,
        requirements_json=extract(body).as_json(),
        requirements_version=EXTRACTOR_VERSION,
    )
    return Posting(**(values | changes))


@pytest.mark.parametrize(
    "question,intent",
    [
        ("Which jobs match skills from my projects?", "projects"),
        ("Which roles match my projects?", "projects"),
        ("Which jobs match skills demonstrated in my projects?", "projects"),
        ("Which skill gaps recur across my strongest matches?", "gaps"),
        ("Which skill gaps recur across jobs in my area?", "gaps"),
        ("Which skill gaps recur across all open jobs in my search area?", "gaps"),
        (
            "Compared with my résumés, which skills do my top matches ask for that I am missing?",
            "gaps",
        ),
        (
            "Compared with my resumes, which skills do my top matches ask for that I am missing?",
            "gaps",
        ),
        ("Which skills do my top matches ask for that I am missing?", "gaps"),
        ("Which skills do my top matches ask for that I am missing in London?", "gaps"),
        ("Which skill gaps recur across my strongest matches at Acme?", None),
        ("Which jobs require Python skills?", None),
        ("Which jobs match my projects in London?", "projects"),
        ("Which jobs match my projects at Acme?", None),
        ("What needs me?", None),
    ],
)
async def test_graph_questions_do_not_swallow_unhandled_constraints(question, intent):
    query = await graph_intent(question)
    assert (query.intent if query else None) == intent


def test_multi_hop_graph_has_source_references_and_private_safe_selection_context():
    repo, cv, job = project(), resume(), posting(company_id=uuid.uuid4())
    graph = build_graph(OWNER, [cv], [repo], [(job, "Example")])
    root = Entity("candidate", str(OWNER))
    node = graph.links(root, "has_project")[0].target
    skill = graph.links(node, "declares")[0].target
    demands = graph.links(skill, "requires", incoming=True)
    assert demands[0].source == Entity("posting", str(job.id))
    assert demands[0].reference == job.url and demands[0].quote
    assert graph.links(Entity("posting", str(job.id)), "posted_by")
    result = graph_result(OWNER, [cv], [repo], [(job, "Example")], "projects")
    assert len(result.evidence) == 1
    assert "source-project" in result.evidence[0].text
    assert repo.url in result.evidence[0].text
    prompt = result.prompt("Which jobs match my projects?")
    assert "project 1" in prompt and "Python" in prompt and "PostgreSQL" in prompt
    assert "source-project" not in prompt and "distinctive private" not in prompt
    assert "source-project" not in result.found.passages[0].excerpt


@pytest.mark.parametrize(
    "changes",
    [
        {"is_fork": True},
        {"is_private": True},
        {"include": False},
        {"candidate_id": uuid.uuid4()},
        {"source": "untrusted"},
        {
            "language": None,
            "dependency_technologies": [],
            "topics_json": ["python"],
            "description": "Expert Python developer",
            "name": "Python project",
        },
    ],
)
def test_unverified_or_excluded_project_facts_never_create_paths(changes):
    result = graph_result(OWNER, [], [project(**changes)], [(posting(), "Example")], "projects")
    assert result.evidence == ()


def test_alternatives_preferences_and_stack_mentions_are_not_direct_requirements():
    jobs = [
        (posting("Requirements\nPython or Java is required."), "Options"),
        (posting("Requirements\nSolid programming skills in Python and/or Golang."), "And/or"),
        (posting("Nice to have\nPython experience."), "Preference"),
        (posting("We build with Python."), "Stack"),
    ]
    result = graph_result(OWNER, [], [project(dependency_technologies=[])], jobs, "projects")
    assert not result.evidence


def test_project_answer_is_readable_and_preserves_skill_attribution():
    first = project(name="Backend", language="Python", dependency_technologies=[])
    second = project(name="Database", language=None, dependency_technologies=["Postgres"])
    result = graph_result(OWNER, [], [first, second], [(posting(), "Example")], "projects")
    answer = result.answer(["P1"])
    assert "Backend Engineer" in answer and "[P1]" in answer
    assert "(Python)" in answer and "(PostgreSQL)" in answer
    assert "Repository declaration" not in answer and "Source text" not in answer
    assert "```" not in answer and "→" not in answer and "Graph retrieval" not in answer
    assert "top open job matches" in answer


def test_old_and_or_classification_is_refreshed_before_building_graph_edges():
    job = posting(
        "Requirements\nSolid programming skills in Python and/or Golang.",
        requirements_json={
            "skills": {
                "required": [
                    {
                        "skill": "python",
                        "quote": "Solid programming skills in Python and/or Golang.",
                        "option": False,
                    }
                ]
            }
        },
        requirements_version=4,
    )
    result = graph_result(OWNER, [], [project()], [(job, "Example")], "projects")
    assert not result.evidence


def test_stale_requirements_refresh_in_memory_and_unread_jobs_remain_unknown():
    stale = posting("Requirements\nRust is required.", requirements_version=0)
    stale.requirements_json = extract("Requirements\nPython is required.").as_json()
    unread = posting(None, requirements_json=None, requirements_version=None)
    result = graph_result(OWNER, [], [project()], [(stale, None), (unread, None)], "projects")
    assert not result.evidence
    assert "readable: 1; unread: 1" in result.summary
    assert stale.requirements_version == 0, "retrieval must not write refreshed extraction"


def test_recurring_gaps_count_jobs_once_and_keep_project_support_separate():
    jobs = [
        (posting("Requirements\nPython and Kubernetes are required."), "Example") for _ in range(2)
    ]
    ignored = resume("Kubernetes", tailored_key="generated")
    other = resume("Kubernetes", candidate_id=uuid.uuid4())
    result = graph_result(
        OWNER,
        [resume(), ignored, other],
        [project(dependency_technologies=["Kubernetes"])],
        jobs,
        "gaps",
    )
    assert len(result.evidence) == 1
    text = result.evidence[0].text
    assert "Kubernetes: directly required by 2 of 2" in text
    assert "public project declares" in text
    assert "Python:" not in text
    assert "not proof you lack" in result.summary


def test_no_resume_does_not_report_every_requirement_as_a_gap():
    result = graph_result(OWNER, [], [], [(posting(), None)] * 2, "gaps")
    assert not result.evidence
    assert "upload a base résumé" in result.summary


async def seed(session):
    user = User(email="graph@example.test")
    session.add(user)
    await session.flush()
    candidate = Candidate(id=OWNER, user_id=user.id, name="Graph Owner", email="owner@example.test")
    company = Company(name="Graph Example", ats_type="greenhouse")
    session.add_all([candidate, company])
    await session.flush()
    profile = Profile(candidate_id=OWNER, label="graph")
    session.add_all([profile, resume(), project()])
    await session.flush()
    jobs = [posting(company_id=company.id) for _ in range(4)]
    jobs[1].closed_at = datetime.now(UTC)
    jobs[2].location = "London, UK"
    session.add_all(jobs)
    await session.flush()
    session.add_all(
        [
            Match(
                profile_id=profile.id,
                posting_id=job.id,
                score=0.9 - n * 0.1,
                decision="skipped" if n == 3 else None,
            )
            for n, job in enumerate(jobs)
        ]
    )
    await session.commit()
    return jobs[0]


async def test_database_scope_deduplicates_profiles_and_excludes_closed_skipped_foreign(
    worker_session,
):
    job = await seed(worker_session)
    second = Profile(candidate_id=OWNER, label="another profile")
    worker_session.add(second)
    await worker_session.flush()
    worker_session.add(Match(profile_id=second.id, posting_id=job.id, score=1.0))
    await worker_session.commit()
    result = await retrieve_graph(worker_session, "projects")
    assert result.found.searched == 1
    assert [p.posting_id for p in result.found.passages] == [job.id]


async def test_area_filter_runs_before_top_100_limit(worker_session):
    job = await seed(worker_session)
    profile = await worker_session.scalar(select(Profile).where(Profile.candidate_id == OWNER))
    outside = [posting(company_id=job.company_id, location="London, UK") for _ in range(101)]
    worker_session.add_all(outside)
    await worker_session.flush()
    worker_session.add_all(
        Match(profile_id=profile.id, posting_id=p.id, score=1.0) for p in outside
    )
    await worker_session.commit()
    result = await retrieve_graph(worker_session, "projects")
    assert [p.posting_id for p in result.found.passages] == [job.id]
    assert result.found.searched == 1

    inside = [posting(company_id=job.company_id) for _ in range(101)]
    worker_session.add_all(inside)
    await worker_session.flush()
    worker_session.add_all(Match(profile_id=profile.id, posting_id=p.id, score=0.5) for p in inside)
    await worker_session.commit()
    bounded = await retrieve_graph(worker_session, "projects")
    assert bounded.found.searched == 100
    assert len(bounded.evidence) == 5


async def test_multiple_candidates_never_mix_personal_evidence(worker_session):
    await seed(worker_session)
    user = User(email="second-graph@example.test")
    worker_session.add(user)
    await worker_session.flush()
    worker_session.add(Candidate(user_id=user.id, name="Other", email="other@example.test"))
    await worker_session.commit()
    result = await retrieve_graph(worker_session, "projects")
    assert not result.found.attempted and not result.evidence
    assert "multiple candidates" in result.summary


@pytest.mark.parametrize("path,provider", [("/chat", "ollama"), ("/chat/stream", "openrouter")])
async def test_chat_selects_graph_paths_with_citations_without_uploading_project_or_resume_text(
    path,
    provider,
    client,
    worker_session,
    monkeypatch,
):
    from apps.api.routers import chat
    from packages.llm.provider import StubProvider

    await seed(worker_session)
    seen = {}

    class Recorder(StubProvider):
        async def complete(self, system, user, **kwargs):
            seen["prompt"] = user
            return '{"ids": ["P1"]}'

    monkeypatch.setattr(chat, "provider_for", lambda selected: (Recorder(), "local-model"))
    response = await client.post(
        path,
        json={
            "message": "Which jobs match skills from my projects?",
            "provider": provider,
            "share_mail": True,
        },
    )
    assert response.status_code == 200, response.text
    if path.endswith("stream"):
        result = json.loads(response.text.splitlines()[-1])["reply"]
    else:
        result = response.json()
    assert "source-project" in result["reply"] and "[P1]" in result["reply"]
    assert result["sources"][0]["cited"] is True
    assert result["unsupported_citations"] == []
    assert "source-project" not in seen["prompt"]
    assert "distinctive private" not in seen["prompt"]
    assert not result["shared_mail"]
    assert result["provider"] == provider
    assert result["retrieval_method"] == "graph"


async def test_no_project_paths_returns_facts_without_calling_a_provider(
    client,
    worker_session,
    monkeypatch,
):
    from apps.api.routers import chat

    await seed(worker_session)
    repo = await worker_session.scalar(select(Project))
    repo.include = False
    await worker_session.commit()

    def forbidden(selected):
        raise AssertionError("No model should be called without graph evidence")

    monkeypatch.setattr(chat, "provider_for", forbidden)
    result = await client.post("/chat", json={"message": "Which jobs match my projects?"})
    assert result.status_code == 200
    assert result.json()["provider"] == "facts"
    assert "No eligible project" in result.json()["reply"]
