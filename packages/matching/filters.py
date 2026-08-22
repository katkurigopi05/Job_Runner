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

#: Seniority ladder, lowest first. Used to reject a mismatch in either
#: direction — an intern posting and a principal posting are both wrong for a
#: mid-level candidate, for opposite reasons.
SENIORITY_LEVELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "intern",
        ("intern", "internship", "co-op", "coop", "apprentice", "apprenticeship", "trainee"),
    ),
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


#: Markers compiled with word boundaries. Plain substring matching read
#: "Internal Tools Engineer" as an internship and "Cooperative Bank Analyst"
#: as one too, because "intern" and "coop" sit inside both. A miscategorised
#: level then feeds `seniority_ok`, which quietly drops the posting for an
#: applicant it actually suited.
#:
#: Trailing punctuation is stripped before the boundary is applied so "sr."
#: still matches "Sr. Engineer" — `\b` after a full stop matches nothing.
_SENIORITY_RES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (
        level,
        re.compile(
            r"\b(?:"
            + "|".join(re.escape(marker.strip().rstrip(".")) for marker in markers)
            + r")\b",
            re.I,
        ),
    )
    for level, markers in SENIORITY_LEVELS
)


def detect_seniority(text: str) -> str | None:
    """The seniority a posting advertises, or None if it does not say."""
    # Highest match wins: "Senior Staff Engineer" is staff-senior, not mid.
    found: str | None = None
    for level, pattern in _SENIORITY_RES:
        if pattern.search(text):
            found = level
    return found


def is_remote(posting: Posting) -> bool:
    haystack = " ".join(filter(None, [posting.location, posting.title]))
    return bool(_REMOTE_RE.search(haystack))


def location_matches(profile: Profile, posting: Posting) -> bool:
    """Whether the owner could plausibly hold this role's location.

    Remote always qualifies. Otherwise the posting's location must mention
    something from the profile's — city or region, matched loosely because
    boards write locations every way imaginable.
    """
    if is_remote(posting):
        return True
    if not profile.location or not posting.location:
        # Nothing to contradict, so do not exclude on a guess.
        return True

    profile_parts = {
        part.strip().lower() for part in re.split(r"[,/|]", profile.location) if part.strip()
    }
    posting_location = posting.location.lower()
    return any(part and part in posting_location for part in profile_parts)


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
