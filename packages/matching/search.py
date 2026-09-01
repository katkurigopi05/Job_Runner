"""Search filters — what the owner wants to see.

Deliberately separate from `Profile`. The profile describes the applicant and
its fields are copied onto real applications (§2.2); a search filter describes
what the owner wants in their feed today. Reading filters off the profile means
narrowing a search also changes what gets typed into a form, and those should
never be one edit.

Filters are hard cuts, not score adjustments. A posting either matches what was
asked for or it does not, and mixing "I do not want this" into a similarity
score makes both harder to reason about — the score answers "how close is this
to my background", the filter answers "did I ask to see it".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from packages.core.models import Posting
from packages.matching.locality import (
    Locality,
    is_domestic,
    is_us_state,
    locality_of,
    location_aliases,
    names_us_region,
    onsite_ok,
    reads_as_remote,
)
from packages.matching.roles import canonical

#: Seniority ladder, low to high. Matching is by position so "senior or above"
#: is expressible without enumerating every title an employer might invent.
SENIORITY_ORDER = ("intern", "junior", "mid", "senior", "staff", "principal")

_SENIORITY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # One rung, several names. An apprenticeship, a co-op and a traineeship
    # are the same tier as an internship — structured entry-level positions —
    # and giving each its own rung would break "at least junior" for no gain.
    (
        "intern",
        re.compile(r"\b(intern(ship)?s?|apprentice(ship)?s?|co[- ]?op|trainee(ship)?s?)\b", re.I),
    ),
    (
        "junior",
        re.compile(r"\b(junior|jr\.?|entry[- ]level|associate|new grad|graduate)\b", re.I),
    ),
    ("principal", re.compile(r"\b(principal|distinguished|fellow)\b", re.I)),
    ("staff", re.compile(r"\b(staff|lead|architect)\b", re.I)),
    ("senior", re.compile(r"\b(senior|sr\.?)\b", re.I)),
]


@dataclass(frozen=True)
class SearchFilters:
    """One search. Every field is optional; an empty filter matches everything."""

    #: All must appear somewhere in title, location, or description.
    keywords: tuple[str, ...] = ()
    #: Any one of these must appear in the location, case-insensitive.
    #:
    #: Matched on a word boundary rather than as a bare substring. "CA" is the
    #: common case and `"ca" in "canada"` is true, so the old substring test
    #: answered a California search with Canadian jobs — and with anything else
    #: whose location happened to contain those two letters.
    locations: tuple[str, ...] = ()
    #: True keeps only remote, False keeps only non-remote, None keeps both.
    remote: bool | None = None
    #: Lowest and highest acceptable rungs of SENIORITY_ORDER.
    min_seniority: str | None = None
    max_seniority: str | None = None
    #: Whether a posting whose rung cannot be read survives a seniority filter.
    #:
    #: False, unlike `allow_unknown_location`, and the asymmetry is deliberate.
    #: A missing *location* is silence — dropping it loses real US jobs for no
    #: evidence. An unreadable *seniority* is different: asking for interns and
    #: being shown every posting whose title does not say is not a filter at
    #: all. Roughly 55% of this corpus has no readable rung, so keeping them
    #: made `min_seniority=intern&max_seniority=intern` return mostly staff
    #: roles — the filter appeared to do nothing, which is worse than being
    #: strict.
    #:
    #: Set True to widen a narrow search back out.
    allow_unknown_seniority: bool = False
    #: Drops anything first seen longer ago than this.
    posted_within_days: int | None = None
    include_closed: bool = False
    #: Hard cut to the United States, and the reason the feed is ordered
    #: California-first. The owner's standing preference rather than a
    #: per-search whim, so `Settings.search_us_only` supplies the default —
    #: but it stays a *filter* (§1), never a term in the score.
    us_only: bool = False
    #: On-site is wanted in California and nowhere else; every other state has
    #: to offer remote. Rides with `us_only` — it is the same standing
    #: preference at a finer grain, and `Settings.search_remote_outside_california`
    #: supplies the default.
    #:
    #: Set False to see on-site roles nationwide, which is what someone willing
    #: to relocate would want.
    remote_outside_california: bool = True
    #: A posting with no location at all is kept by default: silence is not
    #: evidence of a foreign office, and dropping it loses real US jobs. An
    #: unrecognized place *name* is dropped regardless — that is where foreign
    #: postings land. See `Locality.UNPLACED`.
    allow_unknown_location: bool = True

    #: Every reason a posting was dropped, for the feed to explain itself.
    def describe(self) -> list[str]:
        parts: list[str] = []
        if self.keywords:
            parts.append("keywords: " + ", ".join(self.keywords))
        if self.locations:
            parts.append("location: " + " or ".join(self.locations))
        if self.remote is True:
            parts.append("remote only")
        elif self.remote is False:
            parts.append("on-site only")
        if self.min_seniority:
            parts.append(f"{self.min_seniority} or above")
        if self.max_seniority:
            parts.append(f"{self.max_seniority} or below")
        if self.posted_within_days:
            parts.append(f"seen in the last {self.posted_within_days} days")
        if self.include_closed:
            parts.append("including closed")
        if self.us_only:
            parts.append(
                "United States only, California first"
                + (", remote outside California" if self.remote_outside_california else "")
                + ("" if self.allow_unknown_location else ", located postings only")
            )
        return parts

    @property
    def is_empty(self) -> bool:
        return not self.describe()


@dataclass(frozen=True)
class FilterVerdict:
    kept: bool
    #: Why it was dropped. Plural because showing only the first reason invites
    #: fixing one filter and being surprised the posting is still missing.
    reasons: list[str] = field(default_factory=list)


def detect_seniority(text: str) -> str | None:
    """The rung a posting sits on, or None when the title does not say.

    Order matters: "Senior Staff Engineer" is staff, and checking `senior`
    first would file it a rung too low.
    """
    for level, pattern in _SENIORITY_PATTERNS:
        if pattern.search(text):
            return level
    return None


def _location_mentions(location: str, wanted: str) -> bool:
    """Whether `wanted` names a place inside `location`, on a word boundary.

    A bare substring test is wrong for exactly the token people search with
    most: `"ca" in "canada"` is true, and so is `"ca" in "carlsbad"`. Anchoring
    to word boundaries keeps "CA" matching "San Francisco, CA" and "Palo Alto,
    CA, US" while rejecting "Canada".

    Word boundaries alone would be too strict, though: employers write the same
    place both ways, and "CA" must still find "San Francisco, California".
    `location_aliases` supplies the other spelling, so the filter narrows on the
    thing that was actually wrong — Canada — without losing half of California.

    Falls back to a substring when a term has no word characters to anchor on —
    a search for punctuation is odd, but silently matching nothing would be
    worse than the old behaviour it replaces.
    """
    lowered = location.lower()
    matched = False
    for alias in location_aliases(wanted):
        if not re.search(r"\w", alias):
            matched = matched or alias in lowered
            continue
        if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", lowered):
            matched = True
    if not matched:
        return False

    # "CA" is two countries, which is `locality.py`'s own opening warning:
    # `San Jose, CA` is California and `Toronto, ON, CA` is Canada. Word
    # boundaries cannot tell those apart — in both, "CA" is a standalone token.
    #
    # `locality_of` already can, and gets every form right: `Toronto, ON, CA`
    # and `Vancouver, BC, CA` read as elsewhere, while a bare "CA", "Remote —
    # CA" and "Palo Alto, CA, US" read as domestic. So a state search additionally
    # requires the location to read as domestic, rather than this module
    # inventing a second, worse answer to a question already solved next door.
    #
    # Only for state terms: a search for "Toronto" or "London" should find them.
    if is_us_state(wanted):
        return is_domestic(locality_of(location))
    return True


def is_remote(posting: Posting) -> bool:
    """Whether this posting offers remote work. See `locality.reads_as_remote`.

    The definition moved to `locality.py` because `filters.py` had a second,
    weaker copy that read only the title and location. Under the search-area
    rule that disagreement decides whether a posting is kept, so the two
    cannot be allowed to drift.
    """
    return reads_as_remote(
        title=posting.title, location=posting.location, description=posting.description_raw
    )


def matches(posting: Posting, filters: SearchFilters) -> FilterVerdict:
    """Whether a posting is one the owner asked to see, and why not if not."""
    reasons: list[str] = []

    if not filters.include_closed and posting.closed_at is not None:
        reasons.append("posting is closed")

    # Guarded, because building the haystack means lowercasing the *whole*
    # description and it is read nowhere else. Unconditional, it was the single
    # most expensive thing the match feed did: the dashboard's default request
    # sets no keywords, so 27MB of posting text across 4050 rows was lowercased
    # and discarded on every load. Filtering fell from 2.5s to milliseconds.
    if filters.keywords:
        haystack = " ".join(
            part.lower()
            for part in (posting.title, posting.location, posting.description_raw)
            if part
        )
        wanted_role = canonical(posting.title or "")
        for keyword in filters.keywords:
            if keyword.lower() in haystack:
                continue
            # A keyword naming a role matches a title naming the same role, even
            # with no string in common. This filter runs *before* scoring, so a
            # posting dropped here is never scored at all and no embedding
            # backend can recover it: filtering on "software engineer" used to
            # discard "Member of Technical Staff" outright. See matching/roles.py.
            if (asked := canonical(keyword)) is not None and asked == wanted_role:
                continue
            reasons.append(f"missing keyword {keyword!r}")

    if filters.locations:
        # The raw string, not a lowered copy: `locality_of` reads state codes
        # case-sensitively, so lowering here made every state search look
        # foreign and match nothing.
        raw_location = posting.location or ""
        if not any(_location_mentions(raw_location, wanted) for wanted in filters.locations):
            reasons.append("location does not match")

    if filters.us_only:
        where = locality_of(posting.location)
        if where is Locality.UNKNOWN:
            if not filters.allow_unknown_location:
                reasons.append("no location given")
        elif not is_domestic(where):
            reasons.append(f"location {posting.location!r} is outside the United States")
        elif (
            filters.remote_outside_california
            and not onsite_ok(where)
            # A bare "United States" names no region and is no more evidence
            # than silence; `Austin, TX` names a place the owner will not
            # commute to. Only the second is on-site outside California.
            and names_us_region(posting.location)
            and not is_remote(posting)
        ):
            # Domestic, but on-site somewhere the owner will not move to.
            # Rides with `us_only` because it is the same standing preference
            # read one level finer, and separating them would let the feed
            # offer a Chicago desk it already knows is unreachable.
            reasons.append(f"location {posting.location!r} is on-site outside California")

    if filters.remote is not None and is_remote(posting) is not filters.remote:
        reasons.append("remote only" if filters.remote else "on-site only")

    # Only consulted against these two bounds, so with neither set there is
    # nothing to compare it to and no reason to spend the scan.
    if filters.min_seniority or filters.max_seniority:
        # The title, not the description — as this function's own docstring
        # always said it was. Reading the body matched "lead a team" and "our
        # staff" in ordinary prose and filed 54% of this corpus as staff; from
        # the title it is 17%, and interns come out at exactly the number of
        # postings with "intern" in the title.
        level = detect_seniority(posting.title or "")
        if level is None:
            if not filters.allow_unknown_seniority:
                reasons.append("seniority is not stated in the title")
        else:
            rung = SENIORITY_ORDER.index(level)
            if filters.min_seniority and rung < SENIORITY_ORDER.index(filters.min_seniority):
                reasons.append(f"{level} is below {filters.min_seniority}")
            if filters.max_seniority and rung > SENIORITY_ORDER.index(filters.max_seniority):
                reasons.append(f"{level} is above {filters.max_seniority}")

    if filters.posted_within_days is not None:
        cutoff = datetime.now(UTC) - timedelta(days=filters.posted_within_days)
        first_seen = posting.first_seen_at
        if first_seen.tzinfo is None:
            first_seen = first_seen.replace(tzinfo=UTC)
        if first_seen < cutoff:
            reasons.append(f"first seen more than {filters.posted_within_days} days ago")

    return FilterVerdict(kept=not reasons, reasons=reasons)
