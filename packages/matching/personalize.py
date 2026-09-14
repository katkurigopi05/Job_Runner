"""Personalized ordering: explicit adjustments on top of the base score.

`Match.decision` has been captured since `/swipe` shipped and nothing ranked
with it (docs/BACKLOG.md P3). This is the first half of closing that, and it
is deliberately the half that needs no labels:

- **Base and personalized are separate numbers.** `Match.score` is never
  rewritten. The personalized score is computed on read, and the feed orders
  by it only when asked (`rank=personalized`).
- **Every adjustment explains itself.** "+0.10 — requires Python (you prefer
  Python)". An order the owner cannot argue with is one they stop trusting.
- **Nothing is learned without evidence.** Skip reasons produce *suggestions*
  the owner accepts or ignores; accepted ones become explicit preferences like
  any other. There is no trained model here, and `evaluation.py` refuses to
  report one as validated until there are held-out owner labels to validate it
  on — at the 2026-09-14 audit there were zero.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Company, Match, Posting
from packages.matching.locality import reads_as_remote
from packages.matching.skill_vocab import BY_KEY

#: Why a posting was skipped. Fixed, so suggestions can count them.
SKIP_REASONS = (
    "salary",
    "location",
    "seniority",
    "skills",
    "company",
    "role",
    "requirements",
    "duplicate",
    "other",
)

KINDS = ("company", "skill", "title_term", "location_term", "remote")

#: The most one preference may move a score, and the most they may move it
#: together. Bounded so personalization reorders near neighbours rather than
#: burying a strong fit under a pile of small dislikes.
MAX_WEIGHT = 0.3
MAX_TOTAL = 0.5

#: Decisions needed before a pattern is offered as a suggestion.
MIN_EVIDENCE = 3


class PreferenceLike(Protocol):
    kind: str
    value: str
    weight: float


@dataclass(frozen=True)
class Applied:
    kind: str
    value: str
    weight: float
    why: str

    def as_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "value": self.value, "weight": self.weight, "why": self.why}


def _skill_bucket(posting: Posting, key: str) -> str | None:
    skills = (posting.requirements_json or {}).get("skills") or {}
    for bucket in ("required", "preferred", "unclassified"):
        if any(entry["skill"] == key for entry in skills.get(bucket) or []):
            return bucket
    return None


def _mentions(text: str | None, term: str) -> bool:
    return (
        bool(text) and re.search(rf"\b{re.escape(term)}\b", text or "", re.IGNORECASE) is not None
    )


def _applies(preference: PreferenceLike, posting: Posting, company: str | None) -> str | None:
    """Why this preference applies to this posting, or None if it does not."""
    value = preference.value
    if preference.kind == "company":
        if company and company.casefold() == value.casefold():
            return f"company is {company}"
        return None
    if preference.kind == "skill":
        bucket = _skill_bucket(posting, value)
        label = BY_KEY[value].label if value in BY_KEY else value
        if bucket == "unclassified":
            return f"names {label}"
        return f"{bucket} skill: {label}" if bucket else None
    if preference.kind == "title_term":
        return f"title mentions {value!r}" if _mentions(posting.title, value) else None
    if preference.kind == "location_term":
        return f"location mentions {value!r}" if _mentions(posting.location, value) else None
    if preference.kind == "remote":
        remote = reads_as_remote(
            title=posting.title, location=posting.location, description=posting.description_raw
        )
        if value == "remote" and remote:
            return "offers remote work"
        if value == "onsite" and not remote:
            return "on-site"
    return None


def adjust(
    base: float,
    posting: Posting,
    company: str | None,
    preferences: Sequence[PreferenceLike],
) -> tuple[float, list[Applied]]:
    """The personalized score and every adjustment that produced it."""
    applied = [
        Applied(preference.kind, preference.value, preference.weight, why)
        for preference in preferences
        if (why := _applies(preference, posting, company)) is not None
    ]
    total = max(-MAX_TOTAL, min(MAX_TOTAL, sum(item.weight for item in applied)))
    return max(0.0, min(1.0, base + total)), applied


@dataclass(frozen=True)
class Suggestion:
    kind: str
    value: str
    weight: float
    evidence: str

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "value": self.value,
            "weight": self.weight,
            "evidence": self.evidence,
        }


async def suggest(
    session: AsyncSession,
    *,
    profile_id: uuid.UUID | None = None,
    existing: Sequence[PreferenceLike] = (),
) -> list[Suggestion]:
    """Adjustments the owner's own decisions point at. Proposals, never applied here.

    Only patterns with `MIN_EVIDENCE` decisions behind them, and never one the
    owner already has a preference for. A skip for `company` at the same
    company three times is a pattern; one skip is a mood.
    """
    query = (
        select(Match.decision, Match.skip_reason, Posting.requirements_json, Company.name)
        .join(Posting, Posting.id == Match.posting_id)
        .outerjoin(Company, Company.id == Posting.company_id)
        .where(Match.decision.is_not(None))
    )
    if profile_id is not None:
        query = query.where(Match.profile_id == profile_id)
    rows = (await session.execute(query)).all()

    skipped_companies: Counter[str] = Counter()
    kept_companies: Counter[str] = Counter()
    skipped_skills: Counter[str] = Counter()
    for decision, reason, reading, company in rows:
        if decision == "skipped" and reason == "company" and company:
            skipped_companies[company] += 1
        if decision == "interested" and company:
            kept_companies[company] += 1
        if decision == "skipped" and reason in ("skills", "requirements") and reading:
            for entry in (reading.get("skills") or {}).get("required") or []:
                skipped_skills[entry["skill"]] += 1

    have = {(pref.kind, pref.value.casefold()) for pref in existing}
    suggestions: list[Suggestion] = []
    for company, count in skipped_companies.most_common():
        if count >= MIN_EVIDENCE and ("company", company.casefold()) not in have:
            suggestions.append(
                Suggestion(
                    "company",
                    company,
                    -0.1,
                    f"skipped {count} postings at {company} for the company",
                )
            )
    for company, count in kept_companies.most_common():
        if count >= MIN_EVIDENCE and ("company", company.casefold()) not in have:
            suggestions.append(
                Suggestion(
                    "company",
                    company,
                    0.05,
                    f"marked {count} postings at {company} worth applying to",
                )
            )
    for key, count in skipped_skills.most_common(5):
        if count >= MIN_EVIDENCE and ("skill", key) not in have:
            label = BY_KEY[key].label if key in BY_KEY else key
            suggestions.append(
                Suggestion(
                    "skill",
                    key,
                    -0.1,
                    f"skipped {count} postings requiring {label} for skills or requirements",
                )
            )
    return suggestions


__all__ = [
    "KINDS",
    "MAX_TOTAL",
    "MAX_WEIGHT",
    "MIN_EVIDENCE",
    "SKIP_REASONS",
    "Applied",
    "Suggestion",
    "adjust",
    "suggest",
]
