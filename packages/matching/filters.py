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
from packages.matching.locality import locality_of, reachable, reads_as_remote

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
    """Whether this posting offers remote work. See `locality.reads_as_remote`."""
    return reads_as_remote(
        title=posting.title, location=posting.location, description=posting.description_raw
    )


def location_ok(posting: Posting) -> bool:
    """Whether the posting sits inside the owner's search area.

    California on-site or remote, the rest of the United States remote only,
    nothing abroad. `locality.reachable` holds the reasoning; this is the
    plumbing.

    **It does not read the profile, and that is the fix.** The previous
    version compared the posting's location against `profile.location` as a
    substring, after short-circuiting to True for anything remote — so
    "Canada - Remote (ON, AB, BC)" was kept for a US-based owner, as were
    "Remote (India only)" and "Remote - EMEA". Remoteness was allowed to
    override the region, which is backwards: a remote job you are not
    eligible to hold is still a job you cannot hold.

    Reading the profile was the second half of the mistake. §1: a search area
    is the owner's input, not a reading of their profile — conflating them
    means moving house silently rewrites the feed, and it made this filter
    answer a question ("is this near where I live") that nobody had asked in
    place of the one that matters ("did I ask to see this").
    """
    return reachable(locality_of(posting.location), remote=is_remote(posting))


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
    if not location_ok(posting):
        reasons.append(f"location {posting.location!r} is outside the search area")
    if not sponsorship_ok(profile, posting):
        reasons.append("posting states it cannot sponsor and the profile needs sponsorship")
    if not clearance_ok(posting):
        reasons.append("posting requires a security clearance")
    if not seniority_ok(posting, target_seniority):
        reasons.append(f"seniority mismatch for target {target_seniority!r}")

    return FilterResult(passed=not reasons, reasons=reasons)
