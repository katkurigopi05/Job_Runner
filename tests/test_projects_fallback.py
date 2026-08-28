"""Tailoring a résumé that has projects instead of employment.

Every extraction site read `section("experience")` and `publish` wrote back to
a constant of the same name. For a student, a new graduate, or a career changer
that section does not exist — the substance is under Projects — so the bullet
list came back empty, `_tailor` returned None, and the whole of Phase 3 was a
silent no-op. The owner saw a review screen with no diff and no reason, and the
employer received the base résumé.

Found on the owner's own résumé: 0 experience bullets, 37 project lines.
"""

from __future__ import annotations

import pytest

from packages.tailor.bullets import tailorable_bullets, tailorable_section
from packages.tailor.parse import Contact, ParsedResume
from packages.tailor.publish import apply_rewrites
from packages.tailor.rewrite import BulletRewrite, TailorResult


def _resume(**sections: list[str]) -> ParsedResume:
    r = ParsedResume(contact=Contact(name="Owner"), sections=dict(sections))
    r.raw_lines = ["Owner"] + [line for lines in sections.values() for line in lines]
    return r


def test_projects_are_tailored_when_there_is_no_experience() -> None:
    """The case the owner's résumé is in."""
    resume = _resume(projects=["Built a retrieval pipeline in Python."])

    section, bullets = tailorable_bullets(resume)

    assert section == "projects"
    assert bullets == ["Built a retrieval pipeline in Python."]


def test_experience_wins_when_both_exist() -> None:
    """A résumé with both is an employment résumé whose projects are support.

    Rewriting the projects and leaving the jobs untouched would tailor the half
    an employer reads second.
    """
    resume = _resume(
        experience=["Built the billing service."],
        projects=["Built a retrieval pipeline."],
    )

    assert tailorable_section(resume) == "experience"


def test_a_resume_with_neither_reports_nothing_rather_than_guessing() -> None:
    """None means there is nothing to tailor, which is the caller's to report."""
    resume = _resume(education=["BSc Computer Science"])

    assert tailorable_section(resume) is None
    assert tailorable_bullets(resume) == (None, [])


def test_an_empty_section_does_not_win_over_a_full_one() -> None:
    """A heading the parser found but filled with nothing is not a section."""
    resume = _resume(experience=["   ", ""], projects=["Built a retrieval pipeline."])

    assert tailorable_section(resume) == "projects"


def test_rewrites_land_in_the_section_they_came_from() -> None:
    """The failure the shared selector exists to make impossible.

    `apply_rewrites` wrote to a fixed "experience" key. On a projects-only
    résumé that would have invented an experience section containing the
    rewrites and left the projects untouched — the document would gain a
    heading the owner does not have, and the rewrite would be invisible.
    """
    resume = _resume(projects=["Built a retrieval pipeline in Python."])
    result = TailorResult(
        bullets=[
            BulletRewrite(
                original="Built a retrieval pipeline in Python.",
                tailored="Built a data pipeline in Python.",
                changed=True,
            )
        ]
    )

    tailored = apply_rewrites(resume, result)

    assert tailored.section("projects") == ["Built a data pipeline in Python."]
    assert tailored.section("experience") == []


@pytest.mark.asyncio
async def test_comparing_a_projects_resume_no_longer_refuses(db_session, monkeypatch) -> None:
    """`/review`'s Compare panel raised CannotCompare before reaching a model."""
    import uuid

    from packages.core.models import Candidate, Posting, Profile, Resume, User
    from packages.tailor import compare as compare_mod
    from packages.tailor.compare import Candidate as Side
    from packages.tailor.compare import compare_tailorings

    suffix = uuid.uuid4().hex[:8]
    user = User(email=f"o-{suffix}@example.com")
    db_session.add(user)
    await db_session.flush()
    candidate = Candidate(user_id=user.id, name="Owner", email=f"c-{suffix}@example.com")
    db_session.add(candidate)
    await db_session.flush()

    lines = ["Built a retrieval pipeline in Python."]
    resume = Resume(
        candidate_id=candidate.id,
        version=1,
        storage_ref=f"resumes/{candidate.id}/v1/r.txt",
        parsed_json={"raw_lines": lines, "sections": {"projects": lines}},
    )
    db_session.add(resume)
    await db_session.flush()
    profile = Profile(candidate_id=candidate.id, label="p", base_resume_id=resume.id)
    db_session.add(profile)
    posting = Posting(url=f"https://x.test/{suffix}", description_raw="Backend engineer, Python.")
    db_session.add(posting)
    await db_session.flush()

    asked: list[str] = []

    async def fake(session, *, provider_name, **kwargs):  # noqa: ANN001
        asked.append(provider_name)
        return Side(requested=provider_name, changed=1)

    monkeypatch.setattr(compare_mod, "tailor_with", fake)
    monkeypatch.setattr(compare_mod, "cloud_for_tailoring", lambda: "gemini")

    candidates = await compare_tailorings(db_session, profile=profile, posting=posting)

    assert asked == ["ollama", "gemini"]
    assert len(candidates) == 2
