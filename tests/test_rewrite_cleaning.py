"""Stripping the wrapper a model puts around its answer.

Both cases here are from one real run of llama3.1 over the owner's résumé.
One rewrite came back as `Analysis: Designing a validated, reversible…` and
went onto the review screen with the label attached. Another opened with
`Note:`, which the guard refused as a fabricated proper noun — so the bullet
fell back to its original and the stated reason was a word the model had used
as punctuation.
"""

from __future__ import annotations

import pytest

from packages.tailor.rewrite import _clean

ORIGINAL = "Implemented packages for editor state, timelines and playback."


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Analysis: Designing a validated engine.", "Designing a validated engine."),
        ("Note: Shipped the parser.", "Shipped the parser."),
        ("Revised bullet: Shipped the parser.", "Shipped the parser."),
        ("Rewritten bullet: Shipped the parser.", "Shipped the parser."),
        ("• Shipped the parser.", "Shipped the parser."),
        ('"Shipped the parser."', "Shipped the parser."),
    ],
)
def test_a_model_preamble_is_stripped(raw: str, expected: str) -> None:
    assert _clean(raw, ORIGINAL) == expected


def test_a_bullet_that_opens_with_its_own_clause_keeps_it() -> None:
    """The failure an earlier version had: eating content.

    `Built the parser: fast and small.` was stripped to `fast and small.` —
    a real clause read as a label.
    """
    assert (
        _clean("Built the parser: fast and small.", "Built the parser quickly.")
        == "Built the parser: fast and small."
    )


def test_a_label_the_source_also_uses_is_content_not_a_preamble() -> None:
    assert _clean("Python: built the parser", "Python: wrote the parser") == (
        "Python: built the parser"
    )


def test_a_two_word_label_echoing_the_source_survives() -> None:
    assert _clean("Built parser: fast", "Built the parser quickly.") == "Built parser: fast"


def test_an_ordinary_bullet_is_untouched() -> None:
    line = "Built a classification pipeline reaching 0.72 ROC-AUC."
    assert _clean(line, ORIGINAL) == line


def test_a_long_prefix_is_not_treated_as_a_label() -> None:
    """Three words already reaches a real clause, so the pattern stops at two."""
    line = "Designed and shipped: the ingest path."
    assert _clean(line, ORIGINAL) == line
