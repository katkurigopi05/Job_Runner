"""A fact backs only the sections its kind of source may (BACKLOG P5, CLAUDE.md §2.1).

`guard.MAY_BACK` is the table: the résumé's own facts may back any section
(still scoped to their entry), a confirmed internship its own Experience lines
and Skills, a confirmed certification Certifications and Skills, a GitHub
repository's structured record Projects and Skills.

The per-bullet verifier is `rewrite.vet`, which scopes `guard.check` to the
entry the original line came from; a line with no entry (Skills,
Certifications) is checked with `guard.check(..., section=...)`. Offline:
synthetic résumé, no model.
"""

from __future__ import annotations

import pytest

from packages.tailor.guard import (
    MAY_BACK,
    CorpusItem,
    SourceCorpus,
    check,
    may_back,
)
from packages.tailor.parse import ParsedResume
from packages.tailor.rewrite import vet
from packages.tailor.studio import saved_lines_check
from packages.tailor.studio_sources import (
    CertificationFacts,
    InternshipFacts,
    build_source,
    certification_line,
)

EMPLOYER = "Data Analyst, Northwind Freight  Jan 2024 - May 2025"
EMPLOYER_BULLET = "Built weekly freight reports for the operations team."
PROJECT = "Ledgerline — Expense Tracker   [GitHub]"
PROJECT_BULLET = "Built an expense tracker with monthly budget alerts."

ACME = InternshipFacts(
    id="i1",
    company="Acme Robotics",
    role="Software Engineering Intern",
    started="Jun 2025",
    ended="Aug 2025",
    stack=("FastAPI", "PostgreSQL"),
    bullets=("Built a telemetry pipeline that cut report time by 40%.",),
)
GLOBEX = InternshipFacts(
    id="i2",
    company="Globex Labs",
    role="Backend Intern",
    started="Jan 2025",
    ended="Apr 2025",
    stack=("Go", "Redis"),
    bullets=("Wrote a caching layer for the pricing service.",),
)
ACME_BULLET = ACME.bullets[0]
GLOBEX_BULLET = GLOBEX.bullets[0]

CERTIFICATION = CertificationFacts(
    id="c1",
    name="AWS Certified Developer - Associate",
    issuer="Amazon Web Services",
    issued="Jun 2025",
    credential_id="ABC-123",
    # SQL is on the résumé's own Skills line already, so it stays résumé text.
    skills=("AWS Lambda", "Amazon S3", "SQL"),
)

#: What `evidence.linked_evidence` returns for the linked repository: its
#: language and the tools its dependency files declare.
LINKED = {"Ledgerline": "TypeScript\nDocker\nReact"}

#: An unlinked repository's own record (`evidence.project_source_text`).
REPOSITORY = "Streambreaker\nReal-time anomaly detection over Kafka streams\nRust\nkafka"


def base_resume() -> ParsedResume:
    sections = {
        "experience": [EMPLOYER, EMPLOYER_BULLET],
        "projects": [PROJECT, PROJECT_BULLET],
        "skills": ["Languages: Python, SQL"],
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


@pytest.fixture(scope="module")
def source():
    return build_source(base_resume(), [ACME, GLOBEX], [CERTIFICATION])


@pytest.fixture(scope="module")
def corpus(source) -> SourceCorpus:
    return SourceCorpus.from_resume(
        source.resume, REPOSITORY, linked=LINKED, verified=source.verified, kinds=source.kinds
    )


def accepted(corpus: SourceCorpus, original: str, candidate: str) -> bool:
    ok, _, _ = vet(original, candidate, corpus)
    return ok


def refused_for(corpus: SourceCorpus, original: str, candidate: str, term: str) -> None:
    """`candidate` is refused, and for `term`, not for some other check."""
    ok, reason, _ = vet(original, candidate, corpus)
    assert not ok, f"accepted: {candidate!r}"
    assert f"{term!r}: does not appear in" in (reason or ""), reason


# --------------------------------------------------------------------------
# The table, and the tags
# --------------------------------------------------------------------------


def test_the_table_names_exactly_what_each_kind_may_back() -> None:
    assert {
        "resume": None,
        "internship": frozenset({"experience", "skills"}),
        "certification": frozenset({"certifications", "skills"}),
        "github_repo": frozenset({"projects", "skills"}),
    } == MAY_BACK
    assert may_back("resume", "summary")
    assert not may_back("certification", "experience")
    assert not may_back("github_repo", "experience")
    assert not may_back("internship", "projects")


def test_every_unit_carries_where_it_came_from(source, corpus) -> None:
    kinds = {item.text.split("\n", 1)[0]: item for item in corpus.items}

    assert kinds[EMPLOYER_BULLET].source_kind == "resume"
    assert kinds[EMPLOYER_BULLET].section == "experience"
    assert kinds[ACME_BULLET].source_kind == "internship"
    assert kinds[ACME_BULLET].evidence_kind == "internship"
    assert kinds[PROJECT_BULLET].source_kind == "resume"
    assert kinds[PROJECT_BULLET].evidence_kind == "github_repo"
    assert kinds["Streambreaker"].source_kind == "github_repo"
    assert kinds["Streambreaker"].section == "projects"

    # The certification's lines sit apart from the résumé's own shared pool.
    assert set(corpus.shared_by_kind) == {"certification"}
    assert "lambda" in corpus.shared_by_kind["certification"]
    assert "lambda" not in corpus.shared
    assert set(source.kinds.values()) == {"internship", "certification"}


def test_a_caller_that_names_no_kinds_gets_the_corpus_it_always_got() -> None:
    corpus = SourceCorpus.from_resume(base_resume(), linked=LINKED)

    assert {item.source_kind for item in corpus.items} == {"resume"}
    assert corpus.shared_by_kind == {}
    assert corpus.shared_for("experience") == corpus.shared


def test_evidence_of_a_kind_that_may_not_back_the_section_is_not_usable() -> None:
    """Defence in depth: a repository's stack on an Experience unit counts for nothing."""
    docker = frozenset({"docker"})
    on_employer = CorpusItem(
        ref="experience:1",
        text=EMPLOYER_BULLET,
        tokens=frozenset(),
        evidence=docker,
        section="experience",
        evidence_kind="github_repo",
    )
    on_project = CorpusItem(
        ref="projects:1",
        text=PROJECT_BULLET,
        tokens=frozenset(),
        evidence=docker,
        section="projects",
        evidence_kind="github_repo",
    )
    built_by_hand = CorpusItem(ref="x", text="x", tokens=frozenset(), evidence=docker)

    assert on_employer.usable_evidence == frozenset()
    assert on_project.usable_evidence == docker
    # An item built without tags keeps its evidence, as it always did.
    assert built_by_hand.usable_evidence == docker


# --------------------------------------------------------------------------
# Internships
# --------------------------------------------------------------------------


def test_an_internship_backs_its_own_bullets(corpus) -> None:
    assert accepted(
        corpus,
        ACME_BULLET,
        "Built a telemetry pipeline that cut report time by 40%, using FastAPI.",
    )
    assert accepted(
        corpus, GLOBEX_BULLET, "Wrote a caching layer for the pricing service, using Redis."
    )


def test_an_internship_backs_skills_but_not_certifications(corpus) -> None:
    assert check("FastAPI, PostgreSQL, Go, Redis", corpus, section="skills").ok
    assert not check("FastAPI", corpus, section="certifications").ok


@pytest.mark.parametrize(
    ("candidate", "term"),
    [
        ("Built weekly freight reports for the operations team, using FastAPI.", "FastAPI"),
        (
            "Built weekly freight reports for the operations team, cutting report time by 40%.",
            "40%",
        ),
    ],
)
def test_an_internship_never_backs_a_different_employer(corpus, candidate: str, term: str) -> None:
    refused_for(corpus, EMPLOYER_BULLET, candidate, term)


def test_an_internship_never_backs_another_internship(corpus) -> None:
    refused_for(
        corpus,
        GLOBEX_BULLET,
        "Wrote a caching layer for the pricing service, using FastAPI.",
        "FastAPI",
    )
    refused_for(
        corpus, ACME_BULLET, "Built a telemetry pipeline that cut report time by 40% in Go.", "Go"
    )


def test_an_internship_never_backs_a_project(corpus) -> None:
    refused_for(
        corpus,
        PROJECT_BULLET,
        "Built an expense tracker with monthly budget alerts in Redis.",
        "Redis",
    )


# --------------------------------------------------------------------------
# Certifications
# --------------------------------------------------------------------------


def test_a_certification_backs_certifications_and_skills(corpus) -> None:
    assert check(certification_line(CERTIFICATION), corpus, section="certifications").ok
    assert check("Certified: AWS Lambda, Amazon S3", corpus, section="skills").ok


@pytest.mark.parametrize(
    ("original", "candidate", "term"),
    [
        (
            EMPLOYER_BULLET,
            "Built weekly freight reports for the operations team on AWS Lambda.",
            "Lambda",
        ),
        (
            EMPLOYER_BULLET,
            "Built weekly freight reports for the operations team in Amazon S3.",
            "S3",
        ),
        (
            ACME_BULLET,
            "Built a telemetry pipeline that cut report time by 40% on AWS Lambda.",
            "Lambda",
        ),
        (
            PROJECT_BULLET,
            "Built an expense tracker with monthly budget alerts on AWS Lambda.",
            "Lambda",
        ),
    ],
)
def test_a_certification_never_backs_experience_or_projects(
    corpus, original: str, candidate: str, term: str
) -> None:
    refused_for(corpus, original, candidate, term)


def test_a_certified_skill_has_nothing_behind_it_outside_its_sections(corpus) -> None:
    for section in ("experience", "projects", "summary"):
        assert not check("AWS Lambda", corpus, section=section).ok, section


def test_the_tag_is_what_refuses_a_certification_on_an_employer(source) -> None:
    """Control: untagged, the same lines read as the résumé's shared Skills and pass."""
    candidate = "Built weekly freight reports for the operations team on AWS Lambda."
    untagged = SourceCorpus.from_resume(source.resume, verified=source.verified)
    assert accepted(untagged, EMPLOYER_BULLET, candidate)


def test_a_skill_the_resume_lists_itself_stays_the_resumes(corpus) -> None:
    """The certification names SQL too; the résumé's own Skills line still backs it."""
    assert accepted(
        corpus, EMPLOYER_BULLET, "Built weekly freight reports in SQL for the operations team."
    )


def test_a_saved_draft_may_not_put_a_certified_skill_on_an_employer(source) -> None:
    claim = "Built weekly freight reports for the operations team on AWS Lambda."
    sections = {name: list(lines) for name, lines in source.resume.sections.items()}
    sections["experience"] = [
        claim if line == EMPLOYER_BULLET else line for line in sections["experience"]
    ]

    assert saved_lines_check(source, sections, rewrites=[(EMPLOYER_BULLET, claim)]) == [claim]


# --------------------------------------------------------------------------
# GitHub repositories
# --------------------------------------------------------------------------


def test_a_linked_repository_backs_its_project_and_skills(corpus) -> None:
    assert accepted(
        corpus, PROJECT_BULLET, "Built an expense tracker with monthly budget alerts, using Docker."
    )
    assert check("Docker, React, TypeScript", corpus, section="skills").ok


@pytest.mark.parametrize(
    ("original", "candidate"),
    [
        (EMPLOYER_BULLET, "Built weekly freight reports for the operations team, using Docker."),
        (ACME_BULLET, "Built a telemetry pipeline that cut report time by 40%, using Docker."),
    ],
)
def test_a_linked_repository_never_backs_an_employer_or_an_internship(
    corpus, original: str, candidate: str
) -> None:
    refused_for(corpus, original, candidate, "Docker")


def test_a_repositorys_record_backs_projects_and_skills_only(corpus) -> None:
    description = "Real-time anomaly detection over Kafka streams"
    assert check(description, corpus, section="projects").ok
    assert check("Kafka, Rust", corpus, section="skills").ok
    assert not check(description, corpus, section="experience").ok
    refused_for(
        corpus,
        EMPLOYER_BULLET,
        "Built weekly freight reports for the operations team on Kafka.",
        "Kafka",
    )
