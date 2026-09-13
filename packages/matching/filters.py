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

from packages.core.config import get_settings
from packages.core.enums import CitizenshipStatus
from packages.core.models import Posting, Profile
from packages.matching.eligibility import (
    Citizenship,
    PostingEligibility,
    Sponsorship,
    read_posting,
)
from packages.matching.locality import (
    Locality,
    is_domestic,
    locality_of,
    names_us_region,
    onsite_ok,
    reads_as_remote,
)

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
    """Whether this posting offers remote work. See `locality.reads_as_remote`.

    One definition, because there were two: this read only the title and
    location while `search.matches` also read the body, so the same posting
    could be remote in the feed and on-site to the filter that decides whether
    it is scored at all.
    """
    return reads_as_remote(
        title=posting.title, location=posting.location, description=posting.description_raw
    )


def location_matches(
    profile: Profile,
    posting: Posting,
    *,
    remote_outside_california: bool | None = None,
) -> bool:
    """Whether the owner could hold this role's location.

    Two questions, in order: is it abroad, and — if not — is it somewhere the
    owner would go in person.

    ## Country

    This stopped being a substring test. It split the profile's location on
    commas and asked whether any part appeared anywhere in the posting's. On a
    profile reading `san fransico , ca,usa` that produced exactly the wrong
    answer in both directions:

        'Canada'                 -> kept, because 'ca' is inside 'canada'
        'Costa Rica'             -> kept, because 'ca' is inside 'costa'
        'Vancouver, Canada'      -> kept
        'United States - Remote' -> rejected

    Every Canadian and Costa Rican role passed as California while American
    ones did not. It was the top of the owner's match feed after a real crawl:
    four Elastic roles in Canada and a finance manager in Costa Rica.

    Country is read *before* remoteness. "Remote" is not a place, and on a
    foreign posting it does not mean remote-from-anywhere — it means remote
    within that country. `Spain (Remote)`, `United Kingdom (Remote)` and
    `Republic of Ireland (Remote)` were the three highest-scoring matches in
    the owner's feed once the substring bug was fixed, all kept by an
    `is_remote` short-circuit that ran first. `Remote - US or Canada` is
    unaffected: `locality_of` yields a foreign country name to an explicit US
    signal, so it classifies as UNITED_STATES.

    ## Region

    The owner's search area is one sentence — **California on-site or remote,
    the rest of the United States remote only, nothing abroad** — and the
    second clause is a hard filter because they asked for it to be. An on-site
    role in another state is a move, not a commute.

    This overrode an earlier reading, and the earlier one is worth keeping
    visible because it is right in general: *which part* of the US a posting is
    in is normally a ranking question, and `locality.rank` already orders Bay
    Area above California above the rest. Excluding on region hides a Texan
    posting the owner would love in order to keep a Californian one they would
    not. That trade is the owner's to make, and they made it — so it is a
    filter here, and `remote_outside_california=False` (or
    `SEARCH_REMOTE_OUTSIDE_CALIFORNIA=false`) restores ranking-only behaviour
    without a code change.

    ## What still passes

    `UNKNOWN` and `UNPLACED` both pass, for the region check as well as the
    country one. Silence is not evidence, and this is a hard filter: a posting
    whose city no rule recognizes should be ranked down, not hidden. Only an
    explicit foreign signal excludes.

    That matters more than it looks. An earlier version of the region rule
    dropped `UNPLACED`, which made every gap in the hand-written city lists a
    silently discarded job — and 143 of 205 Californian cities did not classify
    from a bare name. The lists are much longer now, but the filter no longer
    depends on their being complete.
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

    where = locality_of(posting.location)
    if where is Locality.ELSEWHERE:
        return False

    if remote_outside_california is None:
        remote_outside_california = get_settings().search_remote_outside_california
    if not remote_outside_california:
        return True

    # UNKNOWN and UNPLACED reach here and are kept: neither is evidence about
    # which part of the country this is, and guessing would hide real jobs.
    if not is_domestic(where):
        return True
    if onsite_ok(where):
        return True

    # `UNITED_STATES` covers two different facts. `Austin, TX` names a place
    # the owner will not commute to; a bare `United States` names no place at
    # all and is no more evidence than silence. Only the first is on-site
    # outside California.
    if not names_us_region(posting.location):
        return True

    return is_remote(posting)


def eligibility_of(posting: Posting) -> PostingEligibility:
    """What this posting states about authorization. See `matching.eligibility`.

    One reading, used by both filters below and by the feed, because two
    readings of the same sentences would eventually disagree — and the one the
    owner sees on the card would stop being the one that decided whether the
    card is there at all.
    """
    return read_posting(posting.description_raw)


def sponsorship_ok(
    profile: Profile, posting: Posting, *, eligibility: PostingEligibility | None = None
) -> bool:
    """Exclude postings that rule out sponsorship when the owner needs it.

    Only an *explicit refusal* excludes. Three things are deliberately not a
    refusal, and each was getting the wrong answer before:

    - **Silence.** A posting that never mentions sponsorship has said nothing.
      §2.2's caution about work authorization applies to reading as much as to
      answering, so nothing is concluded from an absence.
    - **Ambiguity.** "Candidates with or without sponsorship requirements may
      apply" contains the substring `without sponsorship`, and the old pattern
      excluded on it — hiding the postings that go furthest out of their way to
      say sponsorship is not a barrier.
    - **A contradiction.** A posting that both refuses and offers has not
      established a refusal, and a hard filter needs one.

    Conversely a *negated* refusal now excludes, which is the commonest English
    phrasing and the one the literal pattern list could not see: "We do not
    offer visa sponsorship" matched nothing at all.
    """
    if not profile.needs_sponsorship:
        return True
    read = eligibility or eligibility_of(posting)
    return read.sponsorship is not Sponsorship.UNAVAILABLE


def citizenship_ok(
    profile: Profile, posting: Posting, *, eligibility: PostingEligibility | None = None
) -> bool:
    """Exclude postings restricted to a citizenship the owner does not hold.

    A separate filter from `sponsorship_ok` because it is a separate fact.
    "US citizens only" is not a statement about sponsorship: a permanent
    resident needs no sponsorship and still fails it, and no employer
    generosity fixes it. Read through the sponsorship filter it was invisible.

    **An unstated profile never excludes.** `citizenship_status` is NULL on
    every existing row, and dropping a posting on a field nobody filled in
    would hide jobs the owner may well be eligible for. The restriction is
    surfaced instead — `eligibility.PostingEligibility` carries the sentence,
    and the feed shows it. Silence on the owner's side is not a disqualification
    any more than silence on the posting's side is a clearance.
    """
    read = eligibility or eligibility_of(posting)
    if read.citizenship is Citizenship.UNSTATED:
        return True
    if profile.citizenship_status is None:
        return True

    status = profile.citizenship_status
    if status == CitizenshipStatus.US_CITIZEN.value:
        return True
    # A permanent resident passes a citizens-*or-residents* posting and fails a
    # citizens-only one. That distinction is the whole reason the two verdicts
    # are separate values rather than one "restricted" flag.
    return (
        read.citizenship is Citizenship.CITIZENS_OR_RESIDENTS_ONLY
        and status == CitizenshipStatus.PERMANENT_RESIDENT.value
    )


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
    """Run every hard filter, collecting all reasons rather than the first.

    `target_seniority` falls back to the profile's own rung. It lives here
    rather than in `score_and_store` so that every caller of the hard filters
    gets it — until this existed no production path set a target at all, so
    `seniority_ok` returned True in every real run and the rung filter was
    reachable only from the benchmark. NULL still means "do not filter on
    level".
    """
    if target_seniority is None:
        target_seniority = profile.target_seniority

    reasons: list[str] = []

    if require_open and posting.closed_at is not None:
        reasons.append("posting is closed")
    if not location_matches(profile, posting):
        reasons.append(f"location {posting.location!r} is outside the search area")
    # Read once and handed to both: `eligibility_of` scans the description, and
    # this runs over every posting in the corpus on every rescore.
    authorization = eligibility_of(posting)
    if not sponsorship_ok(profile, posting, eligibility=authorization):
        reasons.append("posting states it cannot sponsor and the profile needs sponsorship")
    if not citizenship_ok(profile, posting, eligibility=authorization):
        reasons.append(f"posting is restricted to {authorization.restriction()}")
    if not clearance_ok(posting):
        reasons.append("posting requires a security clearance")
    if not seniority_ok(posting, target_seniority):
        reasons.append(f"seniority mismatch for target {target_seniority!r}")

    return FilterResult(passed=not reasons, reasons=reasons)
