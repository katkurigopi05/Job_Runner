"""Hard filters — the checks that disqualify a posting outright.

Separate from scoring on purpose. A posting that requires a security clearance
the owner does not have is not "a weak match"; it is not a match. Blending
that into a similarity score would let a high keyword overlap paper over a
disqualifier.

Every filter states *why* it excluded something, so a surprising empty feed
can be explained rather than just stared at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from packages.core.models import Posting, Profile
from packages.matching.locality import Locality, is_domestic, locality_of

#: Seniority ladder, lowest first. Used to reject a mismatch in either
#: direction — an intern posting and a principal posting are both wrong for a
#: mid-level candidate, for opposite reasons.
SENIORITY_LEVELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("intern", ("intern", "internship", "co-op", "coop")),
    ("junior", ("junior", "entry level", "entry-level", "graduate", "new grad", "associate")),
    ("mid", ("mid-level", "mid level", "software engineer ii", "engineer ii")),
    ("senior", ("senior", "sr.", "sr ", "lead", "staff")),
    ("principal", ("principal", "distinguished", "architect", "director", "vp", "head of")),
)

_REMOTE_RE = re.compile(r"\bremote\b|\bwork from home\b|\banywhere\b", re.I)
_SPONSORSHIP_RE = re.compile(
    r"no visa sponsorship|not able to sponsor|unable to sponsor|"
    r"without sponsorship|cannot provide sponsorship|no sponsorship",
    re.I,
)
_CLEARANCE_RE = re.compile(r"security clearance|ts/sci|top secret|public trust clearance", re.I)


@dataclass
class FilterResult:
    passed: bool
    #: Why it was excluded — one reason per failing filter.
    reasons: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.passed


def detect_seniority(text: str) -> str | None:
    """The seniority a posting advertises, or None if it does not say."""
    lowered = f" {text.lower()} "
    # Highest match wins: "Senior Staff Engineer" is staff-senior, not mid.
    found: str | None = None
    for level, markers in SENIORITY_LEVELS:
        if any(marker in lowered for marker in markers):
            found = level
    return found


def is_remote(posting: Posting) -> bool:
    haystack = " ".join(filter(None, [posting.location, posting.title]))
    return bool(_REMOTE_RE.search(haystack))


def location_matches(profile: Profile, posting: Posting) -> bool:
    """Whether the owner could plausibly hold this role's location.

    Country only. Which *part* of the United States a posting is in is a
    ranking question, not a disqualifying one — `locality.rank` orders Bay Area
    above California above the rest, and as that module puts it, a Texan
    posting the owner would love should still rank above a Californian one they
    would not. Excluding on it here would hide the first and keep the second.

    ## Why this stopped being a substring test

    It split the profile's location on commas and asked whether any part
    appeared anywhere in the posting's. On a profile reading
    `san fransico , ca,usa` that produced exactly the wrong answer in both
    directions:

        'Canada'                 -> kept, because 'ca' is inside 'canada'
        'Costa Rica'             -> kept, because 'ca' is inside 'costa'
        'Vancouver, Canada'      -> kept
        'United States - Remote' -> rejected

    Every Canadian and Costa Rican role passed as California while American
    ones did not. It was the top of the owner's match feed after a real crawl:
    four Elastic roles in Canada and a finance manager in Costa Rica.

    `packages/matching/locality.py` exists for this and its own docstring names
    the trap — "CA is two countries", so the country has to be decided before
    `CA` is read as a state. It was written and never wired into this filter.

    ## What still passes

    `UNKNOWN` and `UNPLACED` both pass. Silence is not evidence, and this is a
    hard filter: a posting whose city no rule here recognizes should be ranked
    down, not hidden. Only an explicit foreign signal excludes.
    """
    if not profile.location or not posting.location:
        # Nothing to contradict, so do not exclude on a guess.
        return True

    # The profile's own location decides whether this US-shaped reading applies
    # at all. `locality.py` is explicit that it answers one owner's question —
    # United States only, California first — so a profile located outside it
    # gets no opinion from this filter rather than a wrong one.
    if not is_domestic(locality_of(profile.location)):
        return True

    # Country is read *before* remoteness, not after. "Remote" is not a place,
    # and on a foreign posting it does not mean remote-from-anywhere — it means
    # remote within that country. `Spain (Remote)`, `United Kingdom (Remote)`
    # and `Republic of Ireland (Remote)` were the three highest-scoring matches
    # in the owner's feed after the substring bug below was fixed, all kept by
    # an `is_remote` short-circuit that ran first.
    #
    # `Remote - US or Canada` is unaffected: `locality_of` yields a foreign
    # country name to an explicit US signal, so it classifies as UNITED_STATES.
    return locality_of(posting.location) is not Locality.ELSEWHERE


def sponsorship_ok(profile: Profile, posting: Posting) -> bool:
    """Exclude postings that rule out sponsorship when the owner needs it.

    Only fires on an explicit statement in the posting. Silence is not taken
    as either answer — §2.2's caution about work authorization applies to
    inference as much as to answering.
    """
    if not profile.needs_sponsorship:
        return True
    text = posting.description_raw or ""
    return not _SPONSORSHIP_RE.search(text)


def clearance_ok(posting: Posting) -> bool:
    """Exclude roles requiring a clearance. The owner either has one or does
    not, and the profile has no field claiming one — so this always excludes
    rather than guessing."""
    return not _CLEARANCE_RE.search(posting.description_raw or "")


def seniority_ok(posting: Posting, target: str | None, *, tolerance: int = 1) -> bool:
    """Whether the posting's level is within `tolerance` rungs of `target`."""
    if not target:
        return True
    advertised = detect_seniority(" ".join(filter(None, [posting.title, posting.location])))
    if advertised is None:
        return True

    order = [level for level, _ in SENIORITY_LEVELS]
    try:
        distance = abs(order.index(advertised) - order.index(target))
    except ValueError:
        return True
    return distance <= tolerance


def apply_filters(
    profile: Profile,
    posting: Posting,
    *,
    target_seniority: str | None = None,
    require_open: bool = True,
) -> FilterResult:
    """Run every hard filter, collecting all reasons rather than the first."""
    reasons: list[str] = []

    if require_open and posting.closed_at is not None:
        reasons.append("posting is closed")
    if not location_matches(profile, posting):
        reasons.append(f"location {posting.location!r} does not match {profile.location!r}")
    if not sponsorship_ok(profile, posting):
        reasons.append("posting states it cannot sponsor and the profile needs sponsorship")
    if not clearance_ok(posting):
        reasons.append("posting requires a security clearance")
    if not seniority_ok(posting, target_seniority):
        reasons.append(f"seniority mismatch for target {target_seniority!r}")

    return FilterResult(passed=not reasons, reasons=reasons)
