"""The assistant dock shows every posting the search found, not only the cited ones.

Measured on the owner's database with llama3.1. Retrieval was right and the
dock hid it:

    "Are there any roles using Zig?"  retrieved the one Zig posting (Vercel,
                                      "Go, Rust, or Zig in production"); the
                                      model answered "No, there are no roles
                                      using Zig." and cited nothing.
    "Which open roles use Kafka?"     all 5 retrieved postings mention Kafka;
                                      the model cited 2.
    "What jobs are there in London?"  5 London postings; the model named them
                                      without [P1] labels, so none counted as
                                      cited and the dock listed nothing.

The dock listed cited postings only, on the reasoning that listing the rest
"would present context as evidence". That reasoning holds for what sits
directly under the answer, so cited postings still do. The rest are listed
apart, under a label that says the answer did not use them — a search result,
not a citation. Hiding them made the dock as wrong as the model.

These read the TSX, as `test_tracker_columns.py` does: the dashboard has no
JavaScript test runner, and the property is in how the component partitions
its sources.
"""

from __future__ import annotations

from pathlib import Path

ASSISTANT = Path(__file__).resolve().parent.parent / "apps/web/src/components/assistant.tsx"
SOURCE = ASSISTANT.read_text(encoding="utf-8")


def test_an_answer_that_cites_nothing_still_shows_what_the_search_found() -> None:
    assert "if (cited.length === 0) return null" not in SOURCE, (
        "the dock renders nothing when the model cited nothing, which hides "
        "every posting the search found"
    )


def test_uncited_postings_are_listed_and_labelled_as_uncited() -> None:
    assert "!source.cited" in SOURCE, "the dock never selects the uncited sources"
    assert "not cited in the answer" in SOURCE, (
        "uncited postings must be labelled as such, or they read as the answer's evidence"
    )
