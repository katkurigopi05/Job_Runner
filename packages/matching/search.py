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
from packages.matching.locality import Locality, is_domestic, locality_of

#: Seniority ladder, low to high. Matching is by position so "senior or above"
#: is expressible without enumerating every title an employer might invent.
SENIORITY_ORDER = ("intern", "junior", "mid", "senior", "staff", "principal")

_SENIORITY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("intern", re.compile(r"\bintern(ship)?\b", re.I)),
    ("junior", re.compile(r"\b(junior|jr\.?|entry[- ]level|associate|new grad)\b", re.I)),
    ("principal", re.compile(r"\b(principal|distinguished|fellow)\b", re.I)),
    ("staff", re.compile(r"\b(staff|lead|architect)\b", re.I)),
    ("senior", re.compile(r"\b(senior|sr\.?)\b", re.I)),
]

_REMOTE_RE = re.compile(r"\b(remote|work from home|wfh|distributed|anywhere)\b", re.I)
_ONSITE_RE = re.compile(r"\b(on[- ]?site|in[- ]office|hybrid)\b", re.I)


@dataclass(frozen=True)
class SearchFilters:
    """One search. Every field is optional; an empty filter matches everything."""

    #: All must appear somewhere in title, location, or description.
    keywords: tuple[str, ...] = ()
    #: Any one of these must appear in the location. Substring, case-insensitive.
    locations: tuple[str, ...] = ()
    #: True keeps only remote, False keeps only non-remote, None keeps both.
    remote: bool | None = None
    #: Lowest acceptable rung of SENIORITY_ORDER. A posting whose seniority
    #: cannot be read is kept — an unreadable title is not a reason to hide a job.
    min_seniority: str | None = None
    max_seniority: str | None = None
    #: Drops anything first seen longer ago than this.
    posted_within_days: int | None = None
    include_closed: bool = False
    #: Hard cut to the United States, and the reason the feed is ordered
    #: California-first. The owner's standing preference rather than a
    #: per-search whim, so `Settings.search_us_only` supplies the default —
    #: but it stays a *filter* (§1), never a term in the score.
    us_only: bool = False
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


def is_remote(posting: Posting) -> bool:
    haystack = f"{posting.title or ''} {posting.location or ''}"
    if _REMOTE_RE.search(haystack):
        return True
    # A description mentioning remote is weaker evidence than a location that
    # says so, but an explicit on-site marker in the same text overrides it.
    body = posting.description_raw or ""
    return bool(_REMOTE_RE.search(body)) and not _ONSITE_RE.search(haystack)


def matches(posting: Posting, filters: SearchFilters) -> FilterVerdict:
    """Whether a posting is one the owner asked to see, and why not if not."""
    reasons: list[str] = []

    if not filters.include_closed and posting.closed_at is not None:
        reasons.append("posting is closed")

    haystack = " ".join(
        part.lower() for part in (posting.title, posting.location, posting.description_raw) if part
    )
    for keyword in filters.keywords:
        if keyword.lower() not in haystack:
            reasons.append(f"missing keyword {keyword!r}")

    if filters.locations:
        location = (posting.location or "").lower()
        if not any(wanted.lower() in location for wanted in filters.locations):
            reasons.append("location does not match")

    if filters.us_only:
        where = locality_of(posting.location)
        if where is Locality.UNKNOWN:
            if not filters.allow_unknown_location:
                reasons.append("no location given")
        elif not is_domestic(where):
            reasons.append(f"location {posting.location!r} is outside the United States")

    if filters.remote is not None and is_remote(posting) is not filters.remote:
        reasons.append("remote only" if filters.remote else "on-site only")

    level = detect_seniority(f"{posting.title or ''} {posting.description_raw or ''}")
    if level is not None:
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
