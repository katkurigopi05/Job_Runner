"""The remote reader skips a description that cannot declare remote work, with the same verdicts.

`locality._declares_remote` searched every description with `_REMOTE_DECLARED_RE`,
17 alternatives with no common literal, and that was 1.6 ms a description: a count of
remote jobs read 16,195 of them, 31 s a question (2026-11-27). Every alternative needs
one of a few words ("remote", "from home", "from anywhere", "fully distributed",
"home based", "wfh"), so the text it searches is checked for those first, in Rust when
built. A verdict decides whether a posting is in the owner's search area, so it must be
the full search's.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from packages.matching import locality

GOLDEN = Path(__file__).parent / "fixtures" / "golden" / "postings.json"
DESCRIPTIONS = [
    # From tests/test_remote_declared.py: mentions that are not offers.
    "Location: Onsite at our office in Sterling, Virginia. Team members work onsite Tuesdays "
    "through Fridays, with the flexibility to work remotely on Mondays.",
    "Benefits: Remote workspace, internet, and cellphone stipend.",
    "If this role is remote, we have listed the top to the bottom of the possible range.",
    "Annual Work From Anywhere 4 weeks per year.",
    "We support remote work for most roles, but if you prefer in-person work, join us.",
    # Declarations, one or more per alternative.
    "Work location: Remote Travel: May include up to 15% domestic travel.",
    "Location: This is a remote position.",
    "This role is remote and can be executed in the United States.",
    "Location: This role will be performed at one location (Remote)",
    "Remote: Available for candidates outside these locations.",
    "This is a fully remote role.",
    "You may work from anywhere in the United States.",
    "This work can be performed remotely.",
    "Work will be performed from a remote location after onboarding.",
    "This role can have a Hybrid or Remote work arrangement.",
    "Remotely within the US, with travel.",
    "Affirm is proud to be a remote-first company.",
    "We are a fully distributed team across time zones.",
    "Three days a week in office. Other locations are remote.",
    "The position offers working from home.",
    "This is a home-based role.",
    "This is a home based position.",
    "You can work from home.",
    "WFH is available.",
    "Remote work is an option.",
    "This role is remote, based in Dallas.",
    # Traps: Python-only whitespace and case partners inside the words, a denial between.
    "You can work from\x1chome.",
    "We are a fully\u3000distributed team.",
    "We are a fully di\u017ftributed company.",
    "This role is not remote. You can work from home.",
    "We build distributed systems for home automation.",
    "",
]


def golden_texts() -> list[str]:
    postings = json.loads(GOLDEN.read_text())["postings"]
    return [p.get("description_raw") or p.get("description") or "" for p in postings]


def searched_in_full(description: str) -> bool:
    """`_declares_remote` before the word check, kept as the oracle."""
    body = locality._REMOTE_CONDITIONAL_RE.sub(
        " ", locality._REMOTE_DENIAL_RE.sub(" ", description)
    )
    return any(
        not locality._REMOTE_REGIONAL_RE.search(
            body[found.start() : found.end() + locality._REMOTE_REGIONAL_WINDOW]
        )
        for found in locality._REMOTE_DECLARED_RE.finditer(body)
    )


@pytest.mark.parametrize("description", DESCRIPTIONS + golden_texts())
def test_the_verdict_is_the_full_searchs(description: str) -> None:
    assert locality._declares_remote(description) is searched_in_full(description)


def test_the_examples_include_declarations_and_mentions() -> None:
    verdicts = [searched_in_full(d) for d in DESCRIPTIONS]

    assert verdicts.count(True) >= 15
    assert verdicts.count(False) >= 8


class _Unreachable:
    def finditer(self, text: str) -> None:
        raise AssertionError("searched a description that cannot declare remote work")


def test_a_description_without_the_words_is_not_searched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(locality, "_REMOTE_DECLARED_RE", _Unreachable())

    assert not locality._declares_remote("We build distributed systems in Go. Great benefits.")


def test_the_cache_holds_a_whole_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """A count of remote jobs read 16,195 descriptions; 4,096 entries evicted each first."""
    assert locality._REMOTE_PROSE_CACHE_SIZE >= 50_000


def test_with_the_rust_matcher_the_words_are_found_in_rust(rust_batch_class: Any) -> None:
    assert locality._DECLARATION_WORDS.native
