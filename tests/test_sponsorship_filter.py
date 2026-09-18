"""The feed can be asked about work authorization, and an unknown never passes.

`eligibility.py` has read sponsorship and citizenship out of a posting since it
replaced the one regex that was answering three questions. The verdict reached
the card and nothing else: `FILTER_KEYS` had 22 entries covering location,
seniority, pay, skills, education and freshness, and none for the fact that
decides whether an applicant who needs sponsorship can hold the job at all. The
owner could read "states it does not sponsor" one card at a time across a feed
drawn from 14,892 postings, and could not ask the feed for it.

The sentences here are the phrasings `eligibility.py` records getting wrong
before it existed, so this suite fails if that module regresses underneath the
filter as well as if the filter itself does.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from packages.core.models import Posting
from packages.core.schemas_search import FILTER_KEYS
from packages.matching.search import SPONSORSHIP_FILTERS, SearchFilters, matches

NOW = datetime(2026, 9, 18, tzinfo=UTC)

OFFERS = "We are happy to sponsor visas for exceptional candidates."
REFUSES = "We do not offer visa sponsorship."
#: Says sponsorship is not a barrier. The old pattern excluded on it, because
#: `without sponsorship` is a substring — the postings going furthest out of
#: their way to welcome the owner were the ones hidden.
AMBIGUOUS = "Candidates with or without sponsorship requirements may apply."
SILENT = "We are a friendly team building payments infrastructure."
CITIZENS = "Must be a U.S. Citizen or Green Card holder."


def _posting(text: str) -> Posting:
    return Posting(
        url="https://example.test/job",
        title="Backend Engineer",
        location=None,
        description_raw=text,
        first_seen_at=NOW,
        closed_at=None,
    )


def _verdict(text: str, **filters: object):
    return matches(_posting(text), SearchFilters(**filters))


# --- asking for an offer ----------------------------------------------------


def test_a_posting_that_states_it_sponsors_is_kept() -> None:
    assert _verdict(OFFERS, sponsorship="available").kept


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        (REFUSES, "states it does not sponsor"),
        (AMBIGUOUS, "mentions sponsorship without resolving it"),
        (SILENT, "does not say whether it sponsors"),
    ],
)
def test_anything_short_of_an_offer_is_dropped_and_says_why(text: str, reason: str) -> None:
    """An unknown is never a pass (§16), and a hidden job explains itself."""
    verdict = _verdict(text, sponsorship="available")
    assert not verdict.kept
    assert reason in verdict.reasons


def test_silence_is_not_an_offer() -> None:
    """The one this filter exists to get right.

    A posting that never mentions visas has said nothing. Reading that as
    "sponsorship available" would invent the single fact an applicant cannot
    afford to be wrong about — `eligibility.py` refuses to infer it, and a
    filter that inferred it instead would put the mistake one layer up.
    """
    assert not _verdict(SILENT, sponsorship="available").kept


# --- widening it back out ---------------------------------------------------


@pytest.mark.parametrize("text", [OFFERS, AMBIGUOUS, SILENT, CITIZENS])
def test_including_unknowns_drops_only_an_explicit_refusal(text: str) -> None:
    """The pairing someone who needs sponsorship actually uses.

    An explicit offer is rare, so `sponsorship=available` alone hides most of
    the corpus. With the switch the filter keeps everything the posting did not
    rule out, which is the useful question and still never treats silence as an
    offer — it treats it as worth a look.
    """
    assert _verdict(text, sponsorship="available", include_unknown_sponsorship=True).kept


def test_the_switch_does_not_rescue_a_refusal() -> None:
    verdict = _verdict(REFUSES, sponsorship="available", include_unknown_sponsorship=True)
    assert not verdict.kept
    assert "states it does not sponsor" in verdict.reasons


# --- citizenship is a separate fact ----------------------------------------


def test_a_citizenship_restriction_is_not_a_sponsorship_verdict() -> None:
    """`CITIZENS` states a restriction and says nothing about sponsoring.

    A permanent resident needs no sponsorship and still fails it, which is why
    the two have separate switches rather than one authorization filter.
    """
    assert not _verdict(CITIZENS, exclude_citizenship_restricted=True).kept
    assert _verdict(CITIZENS, sponsorship="available", include_unknown_sponsorship=True).kept


def test_the_citizenship_reason_keeps_the_country_capitalised() -> None:
    """`restriction()` returns "US citizens…"; lowercasing it says "us"."""
    verdict = _verdict(CITIZENS, exclude_citizenship_restricted=True)
    assert "restricted to US citizens or permanent residents only" in verdict.reasons


@pytest.mark.parametrize("text", [OFFERS, REFUSES, AMBIGUOUS, SILENT])
def test_an_unrestricted_posting_survives_the_citizenship_filter(text: str) -> None:
    """No `include_unknown_*` twin, because silence already keeps the posting."""
    assert _verdict(text, exclude_citizenship_restricted=True).kept


# --- cost -------------------------------------------------------------------


def test_the_verdict_is_not_read_when_neither_filter_is_set(monkeypatch) -> None:
    """The dashboard's default request asks for neither, and pays nothing.

    Reading the verdict scans the whole description. Unguarded that is the cost
    the keyword filter was guarded against, on every row of every page load.
    """
    import packages.matching.filters as filters_module

    def explode(_posting: Posting) -> None:
        raise AssertionError("eligibility was read for a feed that never asked")

    monkeypatch.setattr(filters_module, "eligibility_of", explode)
    assert matches(_posting(REFUSES), SearchFilters(keywords=("backend",))).kept


# --- the filter is reachable from a real feed -------------------------------


def test_the_keys_are_storable_in_a_saved_search() -> None:
    """A filter absent from FILTER_KEYS is refused rather than stored."""
    assert {
        "sponsorship",
        "include_unknown_sponsorship",
        "exclude_citizenship_restricted",
    } <= FILTER_KEYS


def test_every_control_the_filter_bar_renders_reaches_the_api() -> None:
    """The defect this feature *is*: a computed verdict with no way to ask for it.

    CLAUDE.md records the same shape twice — `citizenship_status` and
    `target_seniority` each shipped with a column, a schema, a filter and tests
    and no control, so neither could fire on real data. The mirror image is a
    control the page forwards nowhere, which looks like it works and silently
    does nothing. Three lists have to agree: the bar writes a key, the page
    forwards it, and the API accepts it.
    """
    web = Path(__file__).resolve().parents[1] / "apps/web/src/app/matches"
    bar = (web / "filter-bar.tsx").read_text()
    page = (web / "page.tsx").read_text()

    written = set(re.findall(r'\bset\(\s*"([a-z_]+)"', bar))
    written |= set(re.findall(r'\bunknownSwitch\(\s*"([a-z_]+)"', bar))
    assert "sponsorship" in written, "the sponsorship control is not in the filter bar"

    block = re.search(r"const FILTER_KEYS = \[(.*?)\] as const;", page, re.S)
    assert block is not None, "the page no longer has a FILTER_KEYS list to read"
    forwarded = set(re.findall(r'"([a-z_]+)"', block.group(1)))

    dropped = written - forwarded
    assert not dropped, f"the filter bar sets keys the page never forwards: {sorted(dropped)}"
    refused = forwarded - FILTER_KEYS
    assert not refused, f"the page sends keys the API refuses: {sorted(refused)}"


def test_an_unknown_sponsorship_value_is_refused_rather_than_ignored() -> None:
    """A vocabulary, not a boolean.

    `seniority_ok` passes everything for a target it cannot place, which is why
    `target_seniority` is an enum: a typo that reads as "no preference" leaves
    the feed looking filtered with nothing to explain it.
    """
    assert SPONSORSHIP_FILTERS == ("available",)
    assert not _verdict(SILENT, sponsorship="available").kept
