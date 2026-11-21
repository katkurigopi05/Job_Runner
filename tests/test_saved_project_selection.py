"""Apply and Studio rank declared facts; prose never creates a requirement edge."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from packages.core.models_skill_links import ProjectSkill
from packages.github import select as selection
from packages.github.client import GitHubClient
from packages.matching.embed import LexicalEmbedder
from packages.tailor.studio import RoleTrack, pick_projects
from tests.test_batch_tailor import _owner
from tests.test_projects import NOW, make_project
from tests.test_resume_studio import base_resume

JOB = "Requirements:\n- Python\n- PostgreSQL\n- Docker"


@pytest.fixture(autouse=True)
def no_github(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Saved project selection must never build a GitHub client")

    monkeypatch.setattr(GitHubClient, "__init__", forbidden)


async def _inventory(session):
    profile = await _owner(session)
    two = make_project(
        candidate_id=profile.candidate_id,
        name="two-saved-skills",
        language="Rust",
        description="A service.",
        pushed_at=NOW - timedelta(days=2000),
        topics_json=[],
    )
    one = make_project(
        candidate_id=profile.candidate_id,
        name="one-saved-skill",
        language="Python",
        dependency_technologies=["Docker"],
        description=JOB,
        pushed_at=NOW,
        topics_json=[],
    )
    session.add_all([two, one])
    await session.flush()
    # Saved links are the ranking input; denormalized project fields must
    # not silently substitute a different declaration set.
    for project, skills in [(two, ["python", "postgresql"]), (one, ["python"])]:
        session.add_all(
            ProjectSkill(project_id=project.id, skill=key, origin="dependency", declared_as=key)
            for key in skills
        )
    await session.flush()
    return profile, two, one


async def test_apply_uses_saved_project_skills_instead_of_prose(db_session, monkeypatch):
    from apps.worker import apply_job

    profile, two, one = await _inventory(db_session)
    monkeypatch.setattr(apply_job, "get_embedder", lambda: LexicalEmbedder())
    chosen = await apply_job._projects_for(db_session, profile.candidate_id, JOB)
    assert [p.id for p in chosen] == [two.id, one.id]


@pytest.mark.parametrize("kind", ["pinned", "fork", "zero", "excluded"])
def test_shared_rule_respects_pins_forks_and_direct_overlap(kind):
    backed = make_project(language="Python", description="A Python service.")
    other = make_project(
        language="Rust",
        description=JOB,
        topics_json=["python", "postgresql", "docker"],
        pinned=kind in {"pinned", "fork"},
        is_fork=kind == "fork",
        include=kind != "excluded",
    )
    declared = {backed.id: {"python"}, other.id: set()}
    chosen = selection.projects_for_posting([other, backed], JOB, declared=declared, now=NOW)
    assert chosen == ([other, backed] if kind == "pinned" else [backed])


def test_unreadable_requirements_keep_the_existing_relevance_order():
    old = make_project(name="old", description="Python data pipeline", language="Python")
    recent = make_project(
        name="recent", description="Python data pipeline", language="Python", pushed_at=NOW
    )
    text = "Our team builds Python data pipelines."
    expected = selection.relevant_for_posting([old, recent], text, now=NOW)
    assert selection.projects_for_posting([old, recent], text, declared={}, now=NOW) == expected


def test_zero_overlap_is_removed_before_taking_the_limit():
    noise = [make_project(language="Rust", description=JOB, stars=500) for _ in range(5)]
    backed = make_project(language="Python", description="A service.")
    declared = {p.id: set() for p in noise} | {backed.id: {"python"}}
    assert selection.projects_for_posting(
        [*noise, backed], JOB, declared=declared, limit=1, now=NOW
    ) == [backed]


def test_studio_saved_facts_use_the_same_coverage_rule():
    two = make_project(name="two", language="Rust", description="A service.")
    one = make_project(name="one", language="Python", dependency_technologies=["Docker"])
    declared = {two.id: {"python", "postgresql"}, one.id: {"python"}}
    picks = pick_projects(
        [one, two],
        base_resume(),
        JOB,
        RoleTrack.SWE,
        declared=declared,
        replace_source_projects=True,
        limit=1,
    )
    assert [p.project.id for p in picks if p.chosen] == [two.id]
    assert picks[0].matched_terms == ("PostgreSQL", "Python")


async def test_saved_selector_does_not_borrow_another_candidates_links(db_session):
    from packages.tailor import project_selection

    profile, two, one = await _inventory(db_session)
    assert await project_selection.saved_projects_for(db_session, uuid.uuid4(), JOB) == []
    chosen = await project_selection.saved_projects_for(db_session, profile.candidate_id, JOB)
    assert [p.id for p in chosen] == [two.id, one.id]
