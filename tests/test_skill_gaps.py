"""What the owner's target postings ask for that their résumés do not show.

The assistant is asked "what am I lagging on?". The honest answer is a count,
not an opinion: the skills the owner's own top matches require or prefer,
read by the same vocabulary from both sides, against the skills their résumés
list. A model then explains the table; it does not get to invent one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from packages.core.models import Candidate, Company, Match, Posting, Profile, Resume, User
from packages.matching.gaps import (
    ResumeSkills,
    TargetPosting,
    gap_report,
    resume_skills,
    target_gaps,
)


def _requirements(required=(), preferred=(), unclassified=()) -> dict:
    def entries(keys):
        return [{"skill": k, "label": k.capitalize(), "quote": f"uses {k}"} for k in keys]

    return {
        "skills": {
            "required": entries(required),
            "preferred": entries(preferred),
            "unclassified": entries(unclassified),
        }
    }


def _by_skill(gaps) -> dict:
    return {g.skill: g for g in gaps}


def test_a_skill_no_resume_lists_is_a_gap_counted_per_posting() -> None:
    resumes = [ResumeSkills("v2", frozenset({"python"}))]
    postings = [
        TargetPosting("Platform Engineer", _requirements(required=["kubernetes", "python"])),
        TargetPosting("SRE", _requirements(required=["kubernetes"], preferred=["kafka"])),
        TargetPosting("Data Engineer", _requirements(preferred=["kubernetes"])),
    ]

    report = gap_report(resumes, postings)

    missing = _by_skill(report.missing)
    assert (missing["kubernetes"].required, missing["kubernetes"].preferred) == (2, 1)
    assert (missing["kafka"].required, missing["kafka"].preferred) == (0, 1)
    assert [g.skill for g in report.missing] == ["kubernetes", "kafka"], "required first"
    assert _by_skill(report.covered)["python"].required == 1
    assert report.read == 3


def test_a_skill_required_and_preferred_in_one_posting_counts_once_as_required() -> None:
    report = gap_report(
        [ResumeSkills("v2", frozenset())],
        [TargetPosting("Backend", _requirements(required=["rust"], preferred=["rust"]))],
    )
    rust = _by_skill(report.missing)["rust"]
    assert (rust.required, rust.preferred) == (1, 0)


def test_a_skill_named_under_no_requirements_heading_is_not_a_demand() -> None:
    """`requirements.py`: "We build with Go and React" in a blurb is not a demand."""
    report = gap_report(
        [ResumeSkills("v2", frozenset())],
        [TargetPosting("Backend", _requirements(unclassified=["rust"]))],
    )
    assert report.missing == ()


def test_a_skill_on_any_resume_is_covered_and_says_which() -> None:
    resumes = [
        ResumeSkills("v1", frozenset({"postgresql"})),
        ResumeSkills("v2", frozenset({"python"})),
    ]
    report = gap_report(resumes, [TargetPosting("API", _requirements(required=["postgresql"]))])

    assert report.missing == ()
    assert _by_skill(report.covered)["postgresql"].on_resumes == ("v1",)


def test_a_posting_with_no_reading_is_counted_not_guessed() -> None:
    report = gap_report(
        [ResumeSkills("v2", frozenset())],
        [TargetPosting("Unread", None), TargetPosting("Read", _requirements(required=["kafka"]))],
    )
    assert (report.read, report.unread) == (1, 1)


def test_resume_skills_uses_the_vocabulary_the_postings_were_read_with() -> None:
    assert resume_skills("Built pipelines in Python on Kubernetes with Kafka") == {
        "python",
        "kubernetes",
        "kafka",
    }


async def test_target_gaps_reads_base_resumes_against_the_top_matches(db_session) -> None:
    """Base résumés only, and the owner's own targets: open, matched, not skipped.

    A tailored résumé is excluded because it was bent toward one posting; a
    skipped match is excluded because the owner already said it is not a target.
    """
    suffix = uuid.uuid4().hex[:8]
    user = User(email=f"o-{suffix}@example.com")
    db_session.add(user)
    await db_session.flush()
    candidate = Candidate(user_id=user.id, name="Owner", email=f"c-{suffix}@example.com")
    db_session.add(candidate)
    await db_session.flush()
    profile = Profile(candidate_id=candidate.id, label="p")
    db_session.add(profile)
    company = Company(name="Acme", ats_type="greenhouse")
    db_session.add(company)
    await db_session.flush()

    def posting(title, requirements, *, closed=False):
        return Posting(
            company_id=company.id,
            url=f"https://boards.greenhouse.io/acme/jobs/{uuid.uuid4().hex[:8]}",
            title=title,
            description_raw=title,
            requirements_json=requirements,
            closed_at=datetime.now(UTC) if closed else None,
        )

    wanted = posting("Data Engineer", _requirements(required=["kafka", "python"]))
    skipped = posting("Skipped", _requirements(required=["rust"]))
    closed = posting("Closed", _requirements(required=["rust"]), closed=True)
    db_session.add_all([wanted, skipped, closed])
    await db_session.flush()

    base = Resume(
        candidate_id=candidate.id,
        version=1,
        storage_ref="r1",
        is_default=True,
        parsed_json={"raw_lines": ["Python developer"]},
    )
    tailored = Resume(
        candidate_id=candidate.id,
        version=2,
        storage_ref="r2",
        parsed_json={"raw_lines": ["Kafka streaming"]},
        tailored_for_posting_id=wanted.id,
    )
    db_session.add_all([base, tailored])
    db_session.add_all(
        [
            Match(profile_id=profile.id, posting_id=wanted.id, score=0.9),
            Match(profile_id=profile.id, posting_id=skipped.id, score=0.8, decision="skipped"),
            Match(profile_id=profile.id, posting_id=closed.id, score=0.7),
        ]
    )
    await db_session.flush()

    report = await target_gaps(db_session)

    assert report.read == 1, "skipped and closed postings are not targets"
    assert [g.skill for g in report.missing] == ["kafka"], "the tailored résumé is not a source"
    assert [g.skill for g in report.covered] == ["python"]
    assert [r.label for r in report.resumes] == ["v1 (default)"]
