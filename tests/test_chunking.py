"""Noun-phrase extraction in the fabrication guard.

§9 Gate 3 says the guard traces "every noun-phrase entity". It traced
capitalization instead, and a lowercase claim was invisible:

    résumé:  "Maintained the billing service and reduced invoice errors."
    rewrite: "...with machine learning."   -> accepted, 0 entities checked
"""

from __future__ import annotations

import pytest

from packages.tailor.chunk import available, claim_words, noun_phrases
from packages.tailor.guard import EntityKind, SourceCorpus, extract_entities
from packages.tailor.rewrite import vet

RESUME = (
    "Experience\n"
    "Acme Corp - Backend Engineer\n"
    "- Maintained the billing service and reduced invoice errors.\n"
)
ORIGINAL = "- Maintained the billing service and reduced invoice errors."

needs_tagger = pytest.mark.skipif(not available(), reason="NLTK tagger data not installed")


def _corpus() -> SourceCorpus:
    return SourceCorpus.from_texts(RESUME)


# --------------------------------------------------------------------------
# The hole this closes
# --------------------------------------------------------------------------


@needs_tagger
@pytest.mark.parametrize(
    ("label", "candidate"),
    [
        ("message queue", "- Maintained the billing service using a message queue."),
        ("machine learning", "- Maintained the billing service with machine learning."),
        ("event sourcing", "- Maintained the billing service with event sourcing."),
        ("kafka topics", "- Maintained the billing service across kafka topics."),
    ],
)
def test_a_lowercase_invention_is_caught(label: str, candidate: str) -> None:
    """None of these are capitalized, numeric or an acronym, so the old
    extractor checked nothing at all."""
    accepted, reason, report = vet(ORIGINAL, candidate, _corpus())

    assert not accepted, label
    assert report.checked > 0
    assert "noun" in (reason or "")


# --------------------------------------------------------------------------
# What it must not break — §2.1 permits rephrasing
# --------------------------------------------------------------------------


@needs_tagger
@pytest.mark.parametrize(
    "candidate",
    [
        "- Maintained the billing service, cutting invoice errors.",
        "- Reduced invoice errors while maintaining the billing service.",
        "- Maintained the billing service; invoice errors fell.",
    ],
)
def test_an_honest_rewrite_still_passes(candidate: str) -> None:
    accepted, reason, _ = vet(ORIGINAL, candidate, _corpus())

    assert accepted, reason


@needs_tagger
def test_verbs_are_free() -> None:
    """ "oversaw" for "maintained" is rephrasing, not invention. Flagging any
    unfamiliar word would have made the tailorer look broken."""
    words = claim_words("- Rebuilt and migrated the billing service.")

    assert "rebuilt" not in [w.lower() for w in words]
    assert "migrated" not in [w.lower() for w in words]
    assert "billing" in [w.lower() for w in words]


@needs_tagger
def test_a_leading_verb_is_not_read_as_a_noun() -> None:
    """Résumé bullets start with a verb, and a sentence-initial word is where
    a tagger is most likely to guess wrong."""
    for bullet in ("- Owned the payments team.", "- Built a Python pipeline."):
        assert claim_words(bullet)[0].lower() not in {"owned", "built"}


# --------------------------------------------------------------------------
# The extractor itself
# --------------------------------------------------------------------------


@needs_tagger
def test_phrases_are_reported_whole() -> None:
    """A finding that says "message queue" is actionable; one that says
    "queue" sends the owner looking for what it meant."""
    assert "message queue" in noun_phrases("Maintained a message queue at scale.")


@needs_tagger
def test_a_phrase_does_not_cross_a_preposition() -> None:
    """ "engineer at Google" is two phrases. Treating it as one would make the
    guard demand the whole span appear verbatim."""
    phrases = noun_phrases("Senior engineer at Google Cloud.")

    assert not any("at" in phrase for phrase in phrases)


@needs_tagger
def test_noun_entities_carry_their_own_kind() -> None:
    kinds = {e.kind for e in extract_entities("Maintained a message queue.")}

    assert EntityKind.NOUN in kinds


def test_the_report_says_which_extractor_ran() -> None:
    """A guard that quietly loses a check is worse than one that never had
    it, because nobody re-reads a green test."""
    from packages.tailor.guard import check

    report = check("anything", _corpus())

    assert report.extractor in {"noun-phrase", "capitalization"}
    if available():
        assert report.extractor == "noun-phrase"


def test_the_chunker_degrades_rather_than_raising(monkeypatch) -> None:
    """A fresh machine should run the suite before it has fetched anything."""
    from packages.tailor import chunk

    monkeypatch.setattr(chunk, "_parser", lambda: None)

    assert chunk.claim_words("a message queue") == []
    assert chunk.noun_phrases("a message queue") == []
    assert not chunk.available()
