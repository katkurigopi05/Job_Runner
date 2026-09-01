"""A rewrite nobody can see is not a rewrite.

The counts on the review and comparison screens are what the owner judges two
models by. On the first real local-vs-cloud run against the owner's résumé, the
cloud side reported 12 rewrites and three of them were punctuation: a colon
added to `Core CS  Data Structures`, two spaces removed from `Cloud Data
Warehousing & BI Analytics   [GitHub]`. Nine were real.

Padding one column's total with whitespace is a verdict about the wrong thing
on a screen built to deliver a verdict.
"""

from __future__ import annotations

import pytest

from packages.tailor.rewrite import is_substantive

# --------------------------------------------------------------------------
# Cosmetic
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("original", "candidate"),
    [
        # Both taken verbatim from the stored comparison.
        (
            "Cloud Data Warehousing & BI Analytics   [GitHub]",
            "Cloud Data Warehousing & BI Analytics [GitHub]",
        ),
        (
            "Core CS  Data Structures & Algorithms (DSA), Software Architecture",
            "Core CS: Data Structures & Algorithms (DSA), Software Architecture",
        ),
        ("Built the ingest path.", "Built the ingest path"),
        ("Built  the   ingest path", "Built the ingest path"),
    ],
)
def test_punctuation_and_spacing_are_not_a_rewrite(original: str, candidate: str) -> None:
    assert not is_substantive(original, candidate)


# --------------------------------------------------------------------------
# Real
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("original", "candidate"),
    [
        (
            "Built a full-stack legal research assistant with a hybrid pipeline.",
            "Built a full-stack legal research application with a hybrid pipeline.",
        ),
        (
            "Integrated specialized HuggingFace models for entity extraction.",
            "Integrated specialized HuggingFace machine learning models for entity extraction.",
        ),
        # Case is deliberately not folded: the résumé writes technology names
        # the way their vendors do, and a model lowercasing one has changed
        # something the owner should get to see.
        ("Built services in Python.", "Built services in python."),
    ],
)
def test_a_change_in_wording_is_a_rewrite(original: str, candidate: str) -> None:
    assert is_substantive(original, candidate)


def test_the_source_line_survives_a_cosmetic_answer() -> None:
    """The document and the count have to agree.

    Keeping a candidate that differs only in punctuation while reporting it as
    unchanged would put a line in the PDF that no diff on the review screen
    accounts for — the same class of defect as the tailored résumé that was
    never uploaded, one screen further in.
    """
    from packages.tailor.guard import SourceCorpus
    from packages.tailor.parse import parse_text
    from packages.tailor.rewrite import vet

    source = parse_text(
        "Projects\nThing — Tool  [GitHub]\nPython\nBuilt the ingest path in Python."
    )
    corpus = SourceCorpus.from_resume(source)
    accepted, _reason, _report = vet(
        "Built the ingest path in Python.", "Built the ingest path in Python", corpus
    )
    # It vets cleanly — it is the same sentence — and the caller is what
    # declines to count it. This asserts the guard is not the thing rejecting.
    assert accepted
