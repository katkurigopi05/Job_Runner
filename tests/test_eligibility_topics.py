"""Work authorization is read only where a posting raises it, with the same verdicts.

`read_posting` looks once over the whole text for "sponsor" and for the words of
citizenship, in Rust when it is built. A posting that says neither, most of the feed,
is not split into sentences, and a half of the reading that cannot find anything is not
run. The feed's sponsorship and citizenship filters exclude on these verdicts, so they
must be exactly the full reading's.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from packages.matching import eligibility
from packages.matching.eligibility import (
    Citizenship,
    Evidence,
    PostingEligibility,
    Sponsorship,
    read_posting,
)

GOLDEN = Path(__file__).parent / "fixtures" / "golden" / "postings.json"
TEXTS = [
    "We do not offer visa sponsorship.",
    "We cannot sponsor now or in the future.",
    "Visa sponsorship is available.",
    "Candidates with or without sponsorship are welcome to apply.",
    "Regardless of work authorization, apply.",
    "US citizens only.",
    "US citizens or permanent residents only.",
    "Must be a U.S. Citizen or Green Card holder.",
    "U.S. citizenship or U.S. permanent residency is required.",
    "Open to US persons and lawful residents (LPR) only.",
    # `re.IGNORECASE` reads these as "sponsor" and "Citizen"; so must the topic check.
    "We do not \u017fponsor visas.",
    "Must be a US C\u0131tizen.",
    "Green\u3000card holders welcome; we cannot sponsor.",
    "Sponsors of our annual conference include several partners.",
    "Our team values collaboration.\nWe build distributed systems in Go.",
    "We cannot\nsponsor. US citizens\nonly.",
]


def golden_texts() -> list[str]:
    postings = json.loads(GOLDEN.read_text())["postings"]
    return [p.get("description_raw") or p.get("description") or "" for p in postings]


def every_sentence_read(text: str | None) -> PostingEligibility:
    """`read_posting` before the topic check, kept as the oracle: every pattern, every sentence."""
    if not text:
        return PostingEligibility()
    found: list[Evidence] = []
    refuses = offers = ambiguous = False
    citizenship = Citizenship.UNSTATED
    for sentence in eligibility._sentences(text):
        if eligibility._MENTIONS_SPONSORSHIP.search(sentence):
            if eligibility._AMBIGUOUS_SPONSORSHIP.search(sentence):
                ambiguous = True
                found.append(Evidence.of("sponsorship_ambiguous", sentence))
            elif eligibility._NO_SPONSORSHIP.search(sentence):
                refuses = True
                found.append(Evidence.of("sponsorship_unavailable", sentence))
            elif eligibility._SPONSORSHIP_AVAILABLE.search(sentence):
                offers = True
                found.append(Evidence.of("sponsorship_available", sentence))
        if citizenship is Citizenship.UNSTATED:
            if eligibility._CITIZENS_OR_RESIDENTS.search(sentence):
                citizenship = Citizenship.CITIZENS_OR_RESIDENTS_ONLY
                found.append(Evidence.of("citizens_or_residents_only", sentence))
            elif eligibility._CITIZENS_ONLY.search(sentence):
                citizenship = Citizenship.CITIZENS_ONLY
                found.append(Evidence.of("citizens_only", sentence))
    if ambiguous or (refuses and offers):
        sponsorship = Sponsorship.AMBIGUOUS
    elif refuses:
        sponsorship = Sponsorship.UNAVAILABLE
    elif offers:
        sponsorship = Sponsorship.AVAILABLE
    else:
        sponsorship = Sponsorship.UNSTATED
    return PostingEligibility(sponsorship, citizenship, tuple(found))


@pytest.mark.parametrize("text", TEXTS + golden_texts())
def test_the_verdict_is_every_sentence_reads(text: str) -> None:
    assert read_posting(text) == every_sentence_read(text)


def test_the_examples_cover_every_verdict() -> None:
    readings = [read_posting(text) for text in TEXTS]

    assert {r.sponsorship for r in readings} == set(Sponsorship)
    assert {r.citizenship for r in readings} == set(Citizenship)


def test_a_posting_that_names_neither_is_not_split_into_sentences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def split(text: str) -> list[str]:
        raise AssertionError("split into sentences")

    monkeypatch.setattr(eligibility, "_sentences", split)

    assert read_posting("We build distributed systems in Go.") == PostingEligibility()


class _Unreachable:
    def search(self, text: str) -> None:
        raise AssertionError("ran a pattern that cannot match")


def test_sponsorship_alone_never_runs_the_citizenship_patterns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(eligibility, "_CITIZENS_OR_RESIDENTS", _Unreachable())
    monkeypatch.setattr(eligibility, "_CITIZENS_ONLY", _Unreachable())

    assert read_posting("We cannot sponsor visas.").sponsorship is Sponsorship.UNAVAILABLE


def test_citizenship_alone_never_reads_for_sponsorship(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(eligibility, "_MENTIONS_SPONSORSHIP", _Unreachable())

    assert read_posting("US citizens only.").citizenship is Citizenship.CITIZENS_ONLY


def test_with_the_rust_matcher_the_topics_are_found_in_rust(rust_batch_class: Any) -> None:
    assert eligibility._TOPICS.native
