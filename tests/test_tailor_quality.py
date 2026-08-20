"""Measuring whether tailoring did anything, as opposed to whether it was safe.

The guard already answers "is this a fabrication". These cover the failures it
cannot see, each of which currently looks identical to success: a rewrite that
returned the source line, a run where every rewrite was refused, and output
that took up none of the posting vocabulary the résumé actually supports.
"""

from __future__ import annotations

from packages.tailor.evaluate import measure
from packages.tailor.guard import SourceCorpus
from packages.tailor.parse import Contact, ParsedResume
from packages.tailor.rewrite import BulletRewrite, TailorResult

BULLETS = [
    "Built backend services in Python for a payments platform.",
    "Designed a Postgres schema handling 40 million rows.",
]


def _corpus() -> SourceCorpus:
    return SourceCorpus.from_resume(
        ParsedResume(
            contact=Contact(name="Fixture Owner"),
            sections={"experience": list(BULLETS)},
            raw_lines=list(BULLETS),
        )
    )


def test_a_run_that_changed_nothing_is_not_healthy() -> None:
    """The silent no-op: accepted, guard-clean, and identical to the source.

    Every existing test passes on this. The application would ship an
    untailored résumé while the review screen reported a successful pass.
    """
    result = TailorResult(
        bullets=[BulletRewrite(original=b, tailored=b, changed=False) for b in BULLETS]
    )

    quality = measure(result, "We want Python and Postgres experience.", _corpus())

    assert quality.changed == 0
    assert quality.unchanged_accepted == 2
    assert not quality.healthy
    assert any("no-op" in problem for problem in quality.problems)


def test_a_run_the_guard_refused_entirely_is_not_healthy() -> None:
    """Byte-identical output to the no-op, arrived at the opposite way.

    Both leave the source text in place. Only the rejection count separates
    "nothing needed changing" from "everything was refused", and nothing
    aggregated it until now.
    """
    result = TailorResult(
        bullets=[
            BulletRewrite(
                original=b, tailored=b, rejected_reason="takes 'fintech' from the posting"
            )
            for b in BULLETS
        ]
    )

    quality = measure(result, "We want Python and Postgres experience.", _corpus())

    assert quality.rejection_rate == 1.0
    assert not quality.healthy


def test_uptake_counts_only_terms_the_rewrite_introduced() -> None:
    """A term already in the source is coincidence, not tailoring.

    Counting it would let a tailorer that changes nothing report perfect
    uptake, which is precisely the number this exists to prevent.
    """
    result = TailorResult(
        bullets=[
            BulletRewrite(
                original="Built backend services in Python for a payments platform.",
                tailored="Built backend services in Python for a payments platform.",
                changed=False,
            )
        ]
    )

    quality = measure(result, "We want Python. We want Python.", _corpus())

    assert quality.terms_taken_up == 0


def test_shrinking_output_is_flagged() -> None:
    """A model dropping detail reads as a worse bullet, not a tighter one."""
    result = TailorResult(
        bullets=[
            BulletRewrite(
                original="Designed a Postgres schema handling 40 million rows.",
                tailored="Designed a schema.",
                changed=True,
            )
        ]
    )

    quality = measure(result, "Postgres experience required.", _corpus())

    assert quality.length_ratio < 0.7
    assert any("length" in problem for problem in quality.problems)


def test_an_empty_run_reports_nothing_rather_than_dividing_by_zero() -> None:
    quality = measure(TailorResult(), "anything", _corpus())

    assert quality.bullets == 0
    assert quality.change_rate == 0.0
    assert quality.uptake_rate == 0.0
