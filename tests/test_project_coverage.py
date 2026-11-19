"""Feed coverage is current direct requirements versus the owner's project declarations."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import event

from packages.core.models import Match, Posting, Project
from packages.core.models_skill_links import PostingSkill, PostingSkillRead, ProjectSkill
from packages.matching.requirements import EXTRACTOR_VERSION


async def seed(session, owner, *, state="current", project_fields=None, count=1):
    project = Project(
        candidate_id=uuid.UUID(owner["candidate_id"]),
        external_id=uuid.uuid4().hex,
        name="Service",
        url="https://github.com/example/service",
        language="Python",
        dependency_technologies=["PostgreSQL"],
        **(project_fields or {}),
    )
    session.add(project)
    await session.flush()
    session.add_all(
        [
            ProjectSkill(project_id=project.id, skill=key, origin=origin, declared_as=label)
            for key, origin, label in [
                ("python", "language", "Python"),
                ("python", "dependency", "Python"),
                ("postgresql", "dependency", "PostgreSQL"),
                ("kubernetes", "dependency", "Kubernetes"),
            ]
        ]
    )
    for i in range(count):
        posting = Posting(
            url=f"https://example.com/{uuid.uuid4()}",
            title="Engineer",
            location="California",
            content_hash="current",
        )
        session.add(posting)
        await session.flush()
        session.add(
            Match(
                profile_id=uuid.UUID(owner["profile_id"]),
                posting_id=posting.id,
                score=0.8 - i / 100,
            )
        )
        if state != "unlinked":
            session.add(
                PostingSkillRead(
                    posting_id=posting.id,
                    content_hash="old" if state == "stale_text" else "current",
                    extractor_version=EXTRACTOR_VERSION - 1
                    if state == "stale_version"
                    else EXTRACTOR_VERSION,
                    readable=True,
                )
            )
            for key in ["python", "postgresql", "kubernetes", "go"]:
                session.add(
                    PostingSkill(
                        posting_id=posting.id,
                        skill=key,
                        strength="preferred" if state == "no_direct" else "required",
                        is_option=False,
                        quote=f"Requires {key}",
                    )
                )
            for key, strength, option, quote in [
                ("rust", "required", True, "Go or Rust"),
                ("react", "preferred", False, "React preferred"),
                ("java", "required", False, ""),
                ("kafka", "named", False, "Our Kafka stack"),
            ]:
                session.add(
                    PostingSkill(
                        posting_id=posting.id,
                        skill=key,
                        strength=strength,
                        is_option=option,
                        quote=quote,
                    )
                )
    await session.commit()


async def test_covered_and_missing_are_declarations_only(
    client, worker_session, complete_candidate
):
    await seed(worker_session, complete_candidate)
    row = (await client.get("/matches")).json()[0]
    assert row["project_coverage"] == {
        "covered": ["PostgreSQL", "Python"],
        "missing": ["Go", "Kubernetes"],
    }
    assert row["score"] == 0.8
    assert row["personalized_score"] is None


@pytest.mark.parametrize("state", ["unlinked", "stale_text", "stale_version", "no_direct"])
async def test_unknown_or_empty_requirements_have_no_coverage(
    client, worker_session, complete_candidate, state
):
    await seed(worker_session, complete_candidate, state=state)
    row = (await client.get("/matches")).json()[0]
    assert row["project_coverage"] is None


@pytest.mark.parametrize(
    "fields", [{"is_fork": True}, {"is_private": True}, {"include": False}, {"source": "manual"}]
)
async def test_ineligible_projects_cannot_cover_requirements(
    client, worker_session, complete_candidate, fields
):
    await seed(worker_session, complete_candidate, project_fields=fields)
    row = (await client.get("/matches")).json()[0]
    assert row["project_coverage"] == {
        "covered": [],
        "missing": ["Go", "Kubernetes", "PostgreSQL", "Python"],
    }


async def test_coverage_queries_are_batched_and_page_bounded(
    client, worker_session, complete_candidate, engine
):
    await seed(worker_session, complete_candidate, count=12)
    queries = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append((statement, parameters))

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        counts = []
        for limit in [1, 12]:
            queries.clear()
            response = await client.get("/matches", params={"limit": limit})
            assert response.status_code == 200
            assert all(
                row["project_coverage"]["covered"] == ["PostgreSQL", "Python"]
                for row in response.json()
            )
            coverage = [q for q in queries if "project_skills" in q[0] or "posting_skills" in q[0]]
            assert len(coverage) == 2
            posting_query = next(q for q in coverage if "posting_skills" in q[0])
            assert len([p for p in posting_query[1] if isinstance(p, uuid.UUID)]) == limit
            counts.append(len(queries))
        assert counts[0] == counts[1]
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
