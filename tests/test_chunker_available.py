"""The guard CI runs must be the guard that ships.

`packages/tailor/chunk.py` falls back to matching on capitalization when the
POS tagger data is absent, and the fallback is deliberate — a fresh machine
should be able to run the suite before it has fetched anything. What it must
not do is fall back *unnoticed* on the one machine whose verdict gates a merge.

It did. CI never fetched the data, so every run graded the code against the
weaker extractor, and two real defects reached main invisible to it: the alias
table worked in only one direction, and `_COMMON_WORDS` was missing the generic
machine nouns. Both refused honest rewrites on a developer box and passed here,
because a lowercase noun carries no entity for the capitalization pass to check.

This is the same bargain `REQUIRE_DB` already makes for the database. Off, the
suite still runs anywhere. On, "unavailable" stops being an acceptable state.
"""

from __future__ import annotations

import os

import pytest

from packages.tailor.chunk import available

REQUIRED = os.environ.get("REQUIRE_CHUNKER") == "1"


@pytest.mark.skipif(not REQUIRED, reason="REQUIRE_CHUNKER is not set")
def test_the_noun_phrase_extractor_is_live() -> None:
    assert available(), (
        "REQUIRE_CHUNKER=1 but the POS tagger data is missing, so the guard "
        "would fall back to capitalization matching and every lowercase claim "
        "would go unchecked. Run `make nltk-data`."
    )


@pytest.mark.skipif(not REQUIRED, reason="REQUIRE_CHUNKER is not set")
def test_the_guard_reports_the_strong_extractor() -> None:
    """Availability is necessary; `check` actually using it is the guarantee.

    `GuardReport.extractor` is what a reader trusts when deciding how much a
    green run proved, so it is asserted here rather than inferred from the
    module-level probe above.
    """
    from packages.tailor.guard import SourceCorpus, check

    report = check(
        "Built services in Python.", SourceCorpus.from_texts("Built services in Python.")
    )
    assert report.extractor == "noun-phrase"
