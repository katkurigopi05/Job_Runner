"""Tests for ATS-safe DOCX export and role-targeted Resume Studio synthesis."""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime

import pytest
from docx import Document

from packages.core.models import Candidate, Profile, Project
from packages.llm.provider import StubProvider
from packages.tailor import edit
from packages.tailor.docx_export import render_docx
from packages.tailor.parse import Contact, ParsedResume, parse_resume
from packages.tailor.studio import (
    CertificationRecord,
    InternshipRecord,
    RoleTrack,
    synthesize_resume,
)


def _sample_project(
    *,
    name: str,
    description: str,
    language: str,
    topics: list[str],
    deps: list[str],
    is_private: bool = False,
    pinned: bool = False,
) -> Project:
    return Project(
        id=uuid.uuid4(),
        candidate_id=uuid.uuid4(),
        source="github",
        external_id=name,
        name=name,
        full_name=f"owner/{name}",
        url=f"https://github.com/owner/{name}",
        description=description,
        language=language,
        topics_json=topics,
        stars=4,
        forks=0,
        is_fork=False,
        is_archived=False,
        is_private=is_private,
        pushed_at=datetime.now(UTC),
        include=True,
        pinned=pinned,
        dependency_technologies=deps,
    )


def _sample_base_resume() -> ParsedResume:
    return edit.rebuild_raw_lines(
        ParsedResume(
            contact=Contact(
                name="Alex Rivera",
                email="alex@example.com",
                phone="+1-555-0100",
                links=["https://github.com/example-dev"],
            ),
            sections={
                "summary": ["Software engineer building backend APIs and agent tooling."],
                "experience": [
                    "Backend Intern, Orbit Labs — May 2025 – Aug 2025",
                    "Python, FastAPI, PostgreSQL",
                    "Built idempotent FastAPI ingestion workers with pytest verification.",
                ],
                "education": [
                    "B.S. Computer Science, State University — 2026",
                ],
                "skills": [
                    "Languages: Python, TypeScript",
                    "Frameworks: FastAPI, Next.js",
                ],
            },
        )
    )


def test_render_docx_is_single_column_and_roundtrips() -> None:
    base = _sample_base_resume()
    base.sections["projects"] = [
        "Harness Eval Suite",
        "Python, pytest, Docker",
        "Built deterministic replay checks for tool-calling evaluation runs.",
    ]
    base.sections["certifications"] = [
        "AWS Certified Solutions Architect — Associate (2025)",
    ]
    draft = edit.rebuild_raw_lines(base)

    payload = render_docx(draft)
    assert len(payload) > 1000

    doc = Document(io.BytesIO(payload))
    assert doc.tables == []

    reparsed = parse_resume(payload, "resume.docx")
    assert reparsed.contact.name == "Alex Rivera"
    assert reparsed.contact.email == "alex@example.com"
    assert "Built idempotent FastAPI ingestion workers" in "\n".join(reparsed.section("experience"))
    assert "Harness Eval Suite" in "\n".join(reparsed.section("projects"))
    assert "AWS Certified Solutions Architect" in "\n".join(reparsed.section("certifications"))
    assert "State University" in "\n".join(reparsed.section("education"))


@pytest.mark.asyncio
async def test_synthesize_resume_merges_private_repos_internships_and_certs() -> None:
    candidate = Candidate(id=uuid.uuid4(), name="Alex Rivera", email="alex@example.com")
    profile = Profile(
        id=uuid.uuid4(),
        candidate_id=candidate.id,
        links_json={"linkedin": "https://linkedin.com/in/example-dev"},
    )
    private_repo = _sample_project(
        name="agent-harness-evals",
        description="Deterministic evaluation harness and sandboxing checks for coding agents",
        language="Python",
        topics=["ai-agents", "evals", "redis"],
        deps=["FastAPI", "Redis", "Docker", "pytest"],
        is_private=True,
        pinned=True,
    )
    public_repo = _sample_project(
        name="query-cache-bench",
        description="Hybrid vector and keyword search benchmark service",
        language="TypeScript",
        topics=["nextjs", "redis"],
        deps=["Next.js", "PostgreSQL"],
        is_private=False,
    )
    internships = [
        InternshipRecord(
            company="Stealth Systems",
            role="Agentic AI Engineering Intern",
            dates="Jun 2025 – Sep 2025",
            location="San Francisco, CA",
            stack=["Python", "Redis", "Docker", "pytest"],
            bullets=[
                "Implemented tool-call trace evaluation and Redis hybrid retrieval checks in CI."
            ],
        )
    ]
    certifications = [
        CertificationRecord(
            name="AWS Certified Developer — Associate",
            issuer="Amazon Web Services",
            date="2025",
            credential_id="AWS-12345",
            skills=["AWS", "Docker"],
        )
    ]
    jd = (
        "Seeking an FDE / Harness Engineer with Python, FastAPI, Redis, Docker, "
        "pytest, and deterministic agent evaluation experience."
    )

    result = await synthesize_resume(
        job_description=jd,
        role_track=RoleTrack.FDE_HARNESS,
        candidate=candidate,
        profile=profile,
        base_resume=_sample_base_resume(),
        projects=[private_repo, public_repo],
        internships=internships,
        certifications=certifications,
        use_llm=False,
        project_limit=4,
    )

    assert result.role_track is RoleTrack.FDE_HARNESS
    assert "agent-harness-evals" in result.selected_projects
    assert result.ats_parse_score > 0.0
    assert "python" in {t.lower() for t in result.matched_terms}
    assert "redis" in {t.lower() for t in result.matched_terms}

    proj_text = "\n".join(result.draft.section("projects"))
    # Private repos do not emit dead public [GitHub] badges.
    assert "agent-harness-evals\n" in proj_text or proj_text.startswith("agent-harness-evals")
    assert "agent-harness-evals   [GitHub]" not in proj_text
    assert "query-cache-bench   [GitHub]" in proj_text

    exp_text = "\n".join(result.draft.section("experience"))
    assert "Agentic AI Engineering Intern, Stealth Systems" in exp_text
    cert_text = "\n".join(result.draft.section("certifications"))
    assert "AWS Certified Developer — Associate" in cert_text
    assert "AWS-12345" in cert_text
    assert "<h2>Projects</h2>" in result.html_preview


@pytest.mark.asyncio
async def test_synthesize_resume_llm_guard_refuses_fabricated_tools() -> None:
    class ScriptedGuardProvider(StubProvider):
        async def complete(
            self,
            system: str,
            user: str,
            *,
            max_tokens: int = 1024,
            temperature: float = 0.2,
        ) -> str:
            self.calls.append((system, user))
            if "Redis" in user:
                return (
                    '{"bullets": ["Implemented tool-call trace evaluation and Redis '
                    'hybrid retrieval checks in CI."]}'
                )
            # Fabricate Kubernetes + Kafka which are absent from the candidate corpus.
            return (
                '{"bullets": ["Migrated FastAPI workers onto Kubernetes clusters and '
                'Kafka streams."]}'
            )

    internships = [
        InternshipRecord(
            company="Stealth Systems",
            role="Cloud & Backend Intern",
            dates="2025",
            stack=["Python", "Redis"],
            bullets=[
                "Built Redis hybrid retrieval checks with FastAPI and pytest.",
            ],
        )
    ]
    jd = "Need Python, FastAPI, Redis, Kubernetes, Kafka."

    result = await synthesize_resume(
        job_description=jd,
        role_track=RoleTrack.CLOUD_DEVOPS,
        base_resume=_sample_base_resume(),
        internships=internships,
        provider=ScriptedGuardProvider(),
        use_llm=True,
    )

    exp_text = "\n".join(result.draft.section("experience"))
    assert "Kubernetes" not in exp_text
    assert "Kafka" not in exp_text
    assert result.bullets_refused >= 1
