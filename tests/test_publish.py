"""The tailored résumé has to become a file, and the right one.

The bug these cover is not "rendering crashes" — it is rendering the *wrong
document* while reporting success. A rewrite applied to the wrong employer's
bullet is a fabrication that every entity check passes, because every fact in
it is true somewhere in the résumé.
"""

from __future__ import annotations

from packages.tailor.parse import Contact, ParsedResume
from packages.tailor.publish import apply_rewrites
from packages.tailor.rewrite import BulletRewrite, TailorResult


def _resume(experience: list[str]) -> ParsedResume:
    return ParsedResume(
        contact=Contact(name="Test Owner", email="owner@example.com"),
        sections={"experience": experience},
        raw_lines=list(experience),
    )


def _result(*pairs: tuple[str, str]) -> TailorResult:
    return TailorResult(
        bullets=[
            BulletRewrite(original=original, tailored=tailored, changed=original != tailored)
            for original, tailored in pairs
        ]
    )


def test_rewrites_land_on_the_bullets_they_were_written_for() -> None:
    resume = _resume(["Built the ingest pipeline.", "Ran the on-call rotation."])
    result = _result(
        ("Built the ingest pipeline.", "Built and scaled the ingest pipeline."),
        ("Ran the on-call rotation.", "Led the on-call rotation."),
    )

    tailored = apply_rewrites(resume, result)

    assert tailored.section("experience") == [
        "Built and scaled the ingest pipeline.",
        "Led the on-call rotation.",
    ]


def test_blank_lines_do_not_consume_a_rewrite() -> None:
    """The tailor was handed non-blank lines only.

    If a blank line ate a result, every bullet after it would shift up by one
    and land under the wrong employer — each sentence still traceable to the
    résumé, and the document still a lie.
    """
    resume = _resume(["Acme — Engineer", "", "Shipped the billing service."])
    result = _result(
        ("Acme — Engineer", "Acme — Senior Engineer"),
        ("Shipped the billing service.", "Shipped and owned the billing service."),
    )

    tailored = apply_rewrites(resume, result)

    assert tailored.section("experience") == [
        "Acme — Senior Engineer",
        "",
        "Shipped and owned the billing service.",
    ]


def test_a_short_result_leaves_the_remaining_bullets_alone() -> None:
    resume = _resume(["First bullet.", "Second bullet.", "Third bullet."])
    result = _result(("First bullet.", "First bullet, rewritten."))

    tailored = apply_rewrites(resume, result)

    assert tailored.section("experience") == [
        "First bullet, rewritten.",
        "Second bullet.",
        "Third bullet.",
    ]


def test_the_source_resume_is_not_mutated() -> None:
    """The parsed source is what the guard checks against.

    Tailoring in place would mean the second posting's rewrite was vetted
    against the first posting's output, and the corpus would drift a little
    further from what the owner actually wrote on every application.
    """
    resume = _resume(["Built the ingest pipeline."])
    result = _result(("Built the ingest pipeline.", "Built and scaled the ingest pipeline."))

    apply_rewrites(resume, result)

    assert resume.section("experience") == ["Built the ingest pipeline."]


def test_rejected_rewrites_render_as_the_original() -> None:
    """A guard rejection is a fallback, and the fallback must reach the page.

    `tailor_bullet` already sets `tailored` to the source line when the guard
    refuses. This pins that the renderer honours it rather than reaching for
    the model's discarded attempt.
    """
    resume = _resume(["Built backend services in Python."])
    result = TailorResult(
        bullets=[
            BulletRewrite(
                original="Built backend services in Python.",
                tailored="Built backend services in Python.",
                rejected_reason="takes 'payment' from the posting",
            )
        ]
    )

    tailored = apply_rewrites(resume, result)

    assert tailored.section("experience") == ["Built backend services in Python."]


def test_a_resume_with_no_experience_section_survives() -> None:
    resume = ParsedResume(contact=Contact(name="Test Owner"), sections={"skills": ["Python"]})

    tailored = apply_rewrites(resume, _result())

    assert tailored.section("skills") == ["Python"]
