"""The Resume Studio adds only what the owner stated, and a model only what the guard allows.

Offline throughout: a synthetic résumé, synthetic projects, StubProvider. The
model scenario is `test_targeted_tailoring.py`'s: a bullet that ran pytest
checks supports "automated testing", and "Kubernetes" is a gap nothing backs.
"""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime

from docx import Document
from docx.oxml.ns import qn

from packages.core.models import Project
from packages.tailor.assemble import assemble_pdf
from packages.tailor.docx_export import render_docx
from packages.tailor.guard import SourceCorpus
from packages.tailor.parse import ParsedResume, parse_resume
from packages.tailor.rewrite import vet
from packages.tailor.studio import (
    ROLE_PRESETS,
    RoleTrack,
    as_printed,
    pick_projects,
    saved_lines_check,
    synthesize,
)
from packages.tailor.studio_sources import (
    CertificationFacts,
    InternshipFacts,
    build_source,
)
from tests.test_targeted_tailoring import ACCEPTED, ADDS_A_GAP, LOADER, Scripted

EMPLOYER = "Data Analyst, Northwind Freight  Jan 2024 - May 2025"
EMPLOYER_BULLET = "Built weekly freight reports for the operations team."
POSTING = (
    "Requirements:\nAutomated testing, FastAPI services, Kubernetes, and AWS Lambda.\n"
    "Experience with Python and Docker.\n"
)

INTERNSHIP = InternshipFacts(
    id="i1",
    company="Acme Robotics",
    role="Software Engineering Intern",
    started="Jun 2025",
    ended="Aug 2025",
    stack=("Python", "FastAPI", "PostgreSQL"),
    bullets=("Built a telemetry pipeline that cut report time by 40%.",),
)
CERTIFICATION = CertificationFacts(
    id="c1",
    name="AWS Certified Developer - Associate",
    issuer="Amazon Web Services",
    issued="Jun 2025",
    credential_id="ABC-123",
    skills=("AWS Lambda", "Python", "Amazon S3"),
)


def base_resume() -> ParsedResume:
    sections = {
        "experience": [EMPLOYER, EMPLOYER_BULLET, LOADER],
        "skills": ["Languages: Python, SQL, pytest"],
        "education": ["B.S. Computer Science, State University  2021 - 2025"],
    }
    raw = ["Fixture Owner", "fixture@example.com"]
    for name, lines in sections.items():
        raw += [name, *lines]
    return ParsedResume.model_validate(
        {
            "contact": {"name": "Fixture Owner", "email": "fixture@example.com", "links": []},
            "sections": sections,
            "raw_lines": raw,
        }
    )


def project(name: str, **fields: object) -> Project:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "candidate_id": uuid.uuid4(),
        "source": "github",
        "external_id": name,
        "name": name,
        "full_name": f"octocat/{name}",
        "url": f"https://github.com/octocat/{name}",
        "description": f"{name} service in Python",
        "language": "Python",
        "topics_json": ["fastapi"],
        "stars": 0,
        "forks": 0,
        "is_fork": False,
        "is_archived": False,
        "is_private": False,
        "pushed_at": datetime.now(UTC),
        "include": None,
        "pinned": False,
        "dependency_technologies": ["FastAPI"],
    }
    values.update(fields)
    return Project(**values)


def test_a_confirmed_internship_is_added_as_the_owner_wrote_it() -> None:
    base = base_resume()
    source = build_source(base, [INTERNSHIP])

    experience = source.resume.section("experience")
    assert experience[-3:] == [
        "Software Engineering Intern, Acme Robotics  Jun 2025 - Aug 2025",
        "Python, FastAPI, PostgreSQL",
        "Built a telemetry pipeline that cut report time by 40%.",
    ]
    # The base is the guard's authority and is kept whole; the additions follow it.
    assert source.resume.raw_lines[: len(base.raw_lines)] == base.raw_lines
    assert source.origin_of(experience[-1]).kind == "internship"
    assert source.origin_of(EMPLOYER_BULLET).kind == "resume"
    assert source.added == ("Software Engineering Intern, Acme Robotics",)


def test_an_open_ended_internship_is_not_called_present() -> None:
    open_ended = InternshipFacts(
        id="i2", company="Acme", role="Intern", started="Jun 2025", stack=(), bullets=()
    )
    assert build_source(base_resume(), [open_ended]).resume.section("experience")[-1] == (
        "Intern, Acme  Jun 2025"
    )


def test_an_internship_already_on_the_resume_is_skipped_not_doubled() -> None:
    listed = InternshipFacts(id="i3", company="Northwind Freight", role="Analyst", started="2024")
    source = build_source(base_resume(), [listed])
    assert source.resume.section("experience") == base_resume().section("experience")
    assert source.skipped == ("Analyst, Northwind Freight",)


def test_internship_tools_back_its_own_bullets_and_no_employers() -> None:
    """The stack is evidence for the internship's lines, never another employer's."""
    source = build_source(base_resume(), [INTERNSHIP])
    corpus = SourceCorpus.from_resume(source.resume, verified=source.verified)

    own = INTERNSHIP.bullets[0]
    claim = "Built a telemetry pipeline that cut report time by 40%, using FastAPI."
    accepted, reason, _ = vet(own, claim, corpus)
    assert accepted, reason

    # Without the confirmed stack the same sentence has nothing behind it.
    unconfirmed = SourceCorpus.from_resume(source.resume)
    assert not vet(own, claim, unconfirmed)[0]

    elsewhere = "Built weekly freight reports for the operations team, using FastAPI."
    refused, why, _ = vet(EMPLOYER_BULLET, elsewhere, corpus)
    assert not refused and why is not None


def test_certified_skills_are_listed_once_and_the_credential_stays_on_its_line() -> None:
    source = build_source(base_resume(), certifications=[CERTIFICATION])

    assert source.resume.section("certifications") == [
        "AWS Certified Developer - Associate, Amazon Web Services (Credential ID ABC-123)  Jun 2025"
    ]
    # Python is already listed, so only the two new skills join the Skills section.
    assert source.resume.section("skills")[-1] == "Certified: AWS Lambda, Amazon S3"


async def test_a_certified_skill_is_covered_and_never_a_rewrite_target() -> None:
    source = build_source(base_resume(), certifications=[CERTIFICATION])
    draft = await synthesize(base_resume(), source, POSTING, RoleTrack.CLOUD_DEVOPS)

    status = {t.term.lower(): t.status.value for t in draft.plan.terms}
    assert status["lambda"] == status["aws"] == "covered"
    assert all("lambda" not in t[0].lower() for b in draft.bullets for t in b.targets)
    # Without the certification the same posting term is a gap.
    plain = await synthesize(base_resume(), build_source(base_resume()), POSTING, RoleTrack.SWE)
    assert {t.term.lower(): t.status.value for t in plain.plan.terms}["lambda"] == "gap"


async def test_the_role_preset_never_reaches_the_page() -> None:
    base = base_resume()
    draft = await synthesize(base, build_source(base), POSTING, RoleTrack.CLOUD_DEVOPS)

    printed = "\n".join(as_printed(draft.resume).raw_lines).lower() + draft.html.lower()
    assert ROLE_PRESETS[RoleTrack.CLOUD_DEVOPS].label.lower() not in printed
    assert "terraform" not in printed and "summary" not in draft.resume.sections


def test_a_private_repository_is_evidence_only_until_the_owner_includes_it() -> None:
    hidden = project("harness", is_private=True)
    picks = {
        p.project.name: p
        for p in pick_projects(
            [hidden],
            base_resume(),
            POSTING,
            RoleTrack.SWE,
            selected_ids=[str(hidden.id)],
        )
    }
    assert not picks["harness"].chosen and picks["harness"].held_back == "evidence only"

    included = project("harness", is_private=True, include=True)
    chosen = pick_projects([included], base_resume(), POSTING, RoleTrack.SWE)
    assert chosen[0].chosen and chosen[0].held_back == ""


async def test_a_printed_private_repository_never_prints_its_address() -> None:
    base = base_resume()
    included = project("harness", is_private=True, include=True)
    draft = await synthesize(base, build_source(base), POSTING, RoleTrack.SWE, projects=[included])

    assert "harness" in draft.html
    assert "github.com/octocat/harness" not in draft.html
    text = "\n".join(as_printed(draft.resume, draft.projects).raw_lines)
    assert "github.com/octocat/harness" not in text


async def test_an_undescribed_repository_gets_no_sentence_written_for_it() -> None:
    base = base_resume()
    bare = project("bare-repo", description=None, include=True)
    draft = await synthesize(base, build_source(base), POSTING, RoleTrack.SWE, projects=[bare])

    assert draft.projects == (bare,)
    assert "Built bare-repo" not in draft.html and "bare-repo service" not in draft.html


async def test_a_stub_model_revision_is_kept_with_its_evidence() -> None:
    base = base_resume()
    provider = Scripted(ACCEPTED)
    draft = await synthesize(base, build_source(base), POSTING, RoleTrack.SWE, provider=provider)

    loader = next(b for b in draft.bullets if b.original == LOADER)
    assert loader.status == "rewritten" and loader.tailored == ACCEPTED
    assert loader.targets == (("automated testing", "pytest", "practice"),)
    assert ACCEPTED in draft.resume.section("experience")
    # Only the bullet with something to gain was sent.
    assert len(provider.calls) == 1 and EMPLOYER_BULLET not in provider.calls[0][1]
    assert draft.calls == 1 and draft.tailored_by == "stub"
    assert draft.ats_after.keywords >= draft.ats_before.keywords


async def test_a_stub_model_that_invents_a_tool_is_refused() -> None:
    base = base_resume()
    draft = await synthesize(
        base, build_source(base), POSTING, RoleTrack.SWE, provider=Scripted(ADDS_A_GAP)
    )

    loader = next(b for b in draft.bullets if b.original == LOADER)
    assert loader.status == "refused" and loader.tailored == LOADER
    assert "Kubernetes" not in "\n".join(draft.resume.section("experience"))


async def test_without_a_model_nothing_is_rewritten_and_the_gaps_are_named() -> None:
    base = base_resume()
    draft = await synthesize(base, build_source(base), POSTING, RoleTrack.SWE)

    assert {b.status for b in draft.bullets} == {"not asked"}
    assert draft.calls == 0 and draft.tailored_by is None
    gaps = {t.term.lower() for t in draft.plan.terms if t.status.value == "gap"}
    assert "kubernetes" in gaps


def test_a_saved_draft_may_hold_only_backed_lines() -> None:
    base = base_resume()
    source = build_source(base, [INTERNSHIP], [CERTIFICATION])
    sections = {name: list(lines) for name, lines in source.resume.sections.items()}
    sections["skills"] = ["Languages: pytest, Python, SQL", *sections["skills"][1:]]
    sections["experience"] = [*sections["experience"], ACCEPTED, "Ran payroll for 4 offices."]

    refused = saved_lines_check(
        source,
        sections,
        rewrites=[(LOADER, ACCEPTED)],
        owner_edits=["Ran payroll for 4 offices."],
    )
    assert refused == []

    fabricated = {
        **sections,
        "experience": [*sections["experience"], "Led a Kubernetes migration."],
    }
    assert saved_lines_check(source, fabricated) == [
        ACCEPTED,
        "Ran payroll for 4 offices.",
        "Led a Kubernetes migration.",
    ]
    # A rewrite of a line that is not the source's is not a rewrite.
    assert saved_lines_check(
        source,
        {"experience": ["Led a Kubernetes migration."]},
        rewrites=[("Led nothing.", "Led a Kubernetes migration.")],
    ) == ["Led a Kubernetes migration."]


async def test_the_docx_is_one_column_and_reads_back_into_the_same_sections() -> None:
    base = base_resume()
    public = project("telemetry", include=True)
    hidden = project("harness", is_private=True, include=True)
    source = build_source(base, [INTERNSHIP], [CERTIFICATION])
    draft = await synthesize(base, source, POSTING, RoleTrack.SWE, projects=[public, hidden])

    data = render_docx(draft.resume, draft.projects)
    document = Document(io.BytesIO(data))
    assert not document.tables
    for section in document.sections:
        columns = section._sectPr.find(qn("w:cols"))
        assert columns is None or columns.get(qn("w:num"), "1") == "1"

    back = parse_resume(data, "studio.docx")
    assert back.section("experience") == draft.resume.section("experience")
    assert back.section("certifications") == draft.resume.section("certifications")
    projects_text = "\n".join(back.section("projects"))
    assert "telemetry — telemetry service in Python" in projects_text
    assert "github.com/octocat/telemetry" in projects_text
    assert "harness" in projects_text and "github.com/octocat/harness" not in data.decode("latin-1")


async def test_the_pdf_carries_every_added_line_and_no_private_address() -> None:
    base = base_resume()
    hidden = project("harness", is_private=True, include=True)
    source = build_source(base, [INTERNSHIP], [CERTIFICATION])
    draft = await synthesize(base, source, POSTING, RoleTrack.SWE, projects=[hidden])

    pdf = assemble_pdf(draft.resume, list(draft.projects))
    text = " ".join(parse_resume(pdf, "studio.pdf").raw_lines)
    assert "Acme Robotics" in text and "cut report time by 40%" in text
    assert "Credential ID ABC-123" in text and "harness" in text
    assert "github.com/octocat/harness" not in text
