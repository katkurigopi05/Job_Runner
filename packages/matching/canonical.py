"""Is this listing the same requisition as that one? Only when confident.

A company's requisition can be listed on its ATS board and again on its own
careers page, or reached through an aggregator. Shown twice, it is decided on
twice and can be applied to twice. Merged wrongly, two real openings collapse
into one and the second is never seen — the worse failure, because nothing
on screen says a job is missing.

So the rules are asymmetric. A merge needs positive evidence; any
contradiction refuses it:

- **Never across companies.**
- **Never within one source.** Two listings on the same board with the same
  title are, far more often than not, two openings — headcount of two, or one
  per team. The board already told us they are separate by giving them
  separate ids.
- **Never when both state requisition ids and they differ.**
- **Never when the owner split them** (`canonical_locked`).
- **Never when a source lists the same title and location more than once.**
  Two identical openings on a board and one copy elsewhere: which one the copy
  belongs to cannot be told, so neither merges — attaching both would put two
  real jobs behind one card. A shared requisition id is exempt, because it
  names the opening.
- **Same requisition id and same title** is a merge.
- **Otherwise** the title and location must match and the descriptions must
  be near-identical (≥ 90% of distinct words shared, both long enough for
  that to mean something).

Grouping only adds a link. Every listing keeps its row, URL, matches and
applications; the feed shows one card and lists every source on it.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlparse

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from packages.core.models import Posting
from packages.core.models_jobs import CanonicalJob
from packages.matching.titles import canonical as title_key

log = structlog.get_logger(__name__)

#: Share of distinct words two descriptions must have in common.
MIN_TEXT_SIMILARITY = 0.9

#: Below this many characters a description is too short for similarity to
#: distinguish a copy from a template.
MIN_TEXT_LENGTH = 200

_REQUISITION = re.compile(
    r"\b(?:req(?:uisition)?(?:\s*(?:id|#|no\.?|number))?|job\s*(?:id|#|number)|posting\s*id)"
    r"\s*[:#]?\s*(?P<id>[A-Z]{0,5}[-_]?\d{3,}[A-Z0-9-]*)",
    re.IGNORECASE,
)
_WORD = re.compile(r"[a-z0-9][a-z0-9+#.-]*")


def requisition_id(text: str | None) -> str | None:
    """A requisition id the posting states about itself, normalized, or None."""
    if not text:
        return None
    match = _REQUISITION.search(text)
    return match.group("id").upper().replace("_", "-") if match else None


def source_key(posting: Posting) -> str:
    """Which listing surface this came from: the ATS and the host serving it."""
    host = urlparse(posting.url or "").netloc.lower().removeprefix("www.")
    return f"{posting.ats_type or 'unknown'}|{host}"


def _location_key(location: str | None) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (location or "").lower()).split())


def text_similarity(a: str | None, b: str | None) -> float:
    words_a, words_b = set(_WORD.findall((a or "").lower())), set(_WORD.findall((b or "").lower()))
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


@dataclass(frozen=True)
class Verdict:
    same: bool
    reason: str
    confidence: float = 0.0
    #: Decided by a requisition id both listings state, which names the opening
    #: and so survives a source listing the same title twice. Never inferred
    #: from `confidence`: identical text scores 1.0 and is not an id.
    by_requisition: bool = False


def same_requisition(a: Posting, b: Posting) -> Verdict:
    """Whether two listings are one requisition, and the rule that decided it."""
    if a.company_id is None or a.company_id != b.company_id:
        return Verdict(False, "different companies")
    if a.canonical_locked or b.canonical_locked:
        return Verdict(False, "kept separate by the owner")
    if source_key(a) == source_key(b):
        return Verdict(False, "the same source lists them separately")
    req_a, req_b = requisition_id(a.description_raw), requisition_id(b.description_raw)
    if req_a and req_b and req_a != req_b:
        return Verdict(False, f"different requisition ids ({req_a}, {req_b})")
    if title_key(a.title) != title_key(b.title):
        return Verdict(False, "different titles")
    if req_a and req_a == req_b:
        return Verdict(
            True,
            f"same requisition id {req_a} and title",
            REQUISITION_CONFIDENCE,
            by_requisition=True,
        )
    if _location_key(a.location) != _location_key(b.location):
        return Verdict(False, "different locations")
    if min(len(a.description_raw or ""), len(b.description_raw or "")) < MIN_TEXT_LENGTH:
        return Verdict(False, "descriptions too short to compare")
    similarity = text_similarity(a.description_raw, b.description_raw)
    if similarity >= MIN_TEXT_SIMILARITY:
        return Verdict(
            True, f"same title and location, {similarity:.0%} of words shared", similarity
        )
    return Verdict(False, f"only {similarity:.0%} of words shared")


@dataclass
class AssignReport:
    attached: int = 0
    groups_created: int = 0
    #: Postings matching members of two different groups — left alone rather
    #: than joining two groups on the strength of one listing.
    ambiguous: list[uuid.UUID] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.attached} listings attached, {self.groups_created} groups created, "
            f"{len(self.ambiguous)} left alone as ambiguous"
        )


async def assign(session: AsyncSession, posting_ids: list[uuid.UUID]) -> AssignReport:
    """Attach each listing to the requisition group it confidently belongs to. Does not commit.

    A company's open listings are loaded and keyed once, not once per listing:
    a first crawl of a large board hands this a thousand new ids at a time.
    """
    report = AssignReport()
    if not posting_ids:
        return report
    wanted = set(posting_ids)
    companies = (
        await session.scalars(select(Posting.company_id).where(Posting.id.in_(wanted)).distinct())
    ).all()
    for company_id in companies:
        if company_id is None:
            continue
        open_listings = (
            await session.scalars(
                select(Posting)
                .where(Posting.company_id == company_id, Posting.closed_at.is_(None))
                # Compared on text, never on vectors: 384 floats per listing,
                # for every open listing at the company, on every crawl.
                .options(defer(Posting.description_embedding))
            )
        ).all()
        by_key: dict[str, list[Posting]] = {}
        for listing in open_listings:
            by_key.setdefault(title_key(listing.title), []).append(listing)
        for key, listings in by_key.items():
            if len(listings) < 2:
                continue
            for posting in listings:
                if posting.id in wanted and not posting.canonical_locked:
                    await _attach(session, posting, key, listings, report)
    await session.flush()
    if report.attached:
        log.info("canonical_assigned", summary=report.summary())
    return report


#: Confidence of a merge decided by a shared requisition id.
REQUISITION_CONFIDENCE = 0.99


def _rivals(copy: Posting, source: str, listings: list[Posting]) -> int:
    """How many listings on `source` the `copy` would merge with."""
    return sum(
        1
        for other in listings
        if other.id != copy.id
        and source_key(other) == source
        and same_requisition(copy, other).same
    )


async def _attach(
    session: AsyncSession,
    posting: Posting,
    key: str,
    listings: list[Posting],
    report: AssignReport,
) -> None:
    matches = [
        (other, verdict)
        for other in listings
        if other.id != posting.id and (verdict := same_requisition(posting, other)).same
    ]
    # Text alone cannot pair a copy with one of several identical openings: if
    # the copy matches two listings on this source, or this listing matches two
    # on the copy's source, which one it belongs to is unknowable, so neither
    # merges. A second, *different* opening on the same board does not count.
    matches = [
        (other, verdict)
        for other, verdict in matches
        if verdict.by_requisition
        or (
            _rivals(other, source_key(posting), listings) <= 1
            and _rivals(posting, source_key(other), listings) <= 1
        )
    ]
    if not matches:
        return
    groups = {other.canonical_job_id for other, _ in matches if other.canonical_job_id}
    if posting.canonical_job_id:
        groups.add(posting.canonical_job_id)
    if len(groups) > 1:
        report.ambiguous.append(posting.id)
        return

    group_id = next(iter(groups), None)
    group = await session.get(CanonicalJob, group_id) if group_id else None
    if group is None:
        group = CanonicalJob(
            company_id=posting.company_id,
            title_key=key[:300],
            requisition_id=requisition_id(posting.description_raw),
            evidence_json=[],
        )
        session.add(group)
        await session.flush()
        report.groups_created += 1

    evidence = list(group.evidence_json or [])
    for member, verdict in [(posting, matches[0][1]), *matches]:
        if member.canonical_job_id == group.id:
            continue
        member.canonical_job_id = group.id
        report.attached += 1
        evidence.append(
            {
                "posting_id": str(member.id),
                "source": source_key(member),
                "reason": verdict.reason,
                "confidence": round(verdict.confidence, 3),
            }
        )
    group.evidence_json = evidence


async def split(session: AsyncSession, posting: Posting) -> None:
    """Take a listing out of its group for good. Does not commit."""
    posting.canonical_job_id = None
    posting.canonical_locked = True
    await session.flush()


__all__ = [
    "MIN_TEXT_SIMILARITY",
    "AssignReport",
    "Verdict",
    "assign",
    "requisition_id",
    "same_requisition",
    "source_key",
    "split",
    "text_similarity",
]
