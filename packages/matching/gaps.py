"""What the owner's target postings ask for that their résumés do not show.

Asked "what am I lagging on?", the assistant needs a count rather than an
opinion. Both sides are read with one vocabulary, `skill_vocab`: the postings
at ingestion (`requirements.py`, stored in `requirements_json`) and the résumés
here. Reading them with two different vocabularies would report a gap wherever
the two disagreed about a name.

Three rules carry the weight:

- **Only demands count.** A skill a posting names under no requirements heading
  is `unclassified`, which `requirements.py` records is not a demand — "we
  build with Go" in a company blurb. Counting it would report the employer's
  stack as the owner's shortfall.
- **One count per posting, at its strongest.** A skill both required and
  preferred in one posting is one required mention, not two.
- **A skill on any base résumé is covered.** The owner keeps more than one, and
  a skill they have written down somewhere is not something they lack. Tailored
  résumés are not read: each was bent toward one posting.

Only skill names and counts leave this module, never résumé text. The chat
context it feeds may go to a remote provider the owner picked, and §2.8 keeps
résumé text on this machine except for tailoring; a skill list is the smallest
thing that answers the question.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Match, Posting, Resume
from packages.matching.skill_vocab import BY_KEY, find_skills

#: The head of the feed: what the owner is actually aiming at, not the long
#: tail of postings that barely matched.
TARGET_POSTINGS = 100


@dataclass(frozen=True)
class ResumeSkills:
    label: str
    skills: frozenset[str]


@dataclass(frozen=True)
class TargetPosting:
    title: str
    #: `Posting.requirements_json`; None when the posting has not been read.
    requirements: Mapping[str, Any] | None


@dataclass(frozen=True)
class SkillDemand:
    skill: str
    label: str
    required: int
    preferred: int
    #: Labels of the résumés that list it; empty for a gap.
    on_resumes: tuple[str, ...]
    #: One posting asking for it, so the count can be checked by a person.
    example: str


@dataclass(frozen=True)
class GapReport:
    resumes: tuple[ResumeSkills, ...]
    #: Postings whose requirements were read, and those that were not. An
    #: unread posting asks for nothing as far as this report knows, which is
    #: different from asking for nothing.
    read: int
    unread: int
    missing: tuple[SkillDemand, ...]
    covered: tuple[SkillDemand, ...]


def resume_skills(text: str) -> frozenset[str]:
    """The vocabulary skills a résumé names, line by line as postings are read."""
    return frozenset(skill.key for line in text.splitlines() for skill in find_skills(line))


def skill_label(key: str) -> str:
    return BY_KEY[key].label if key in BY_KEY else key


def asked_and_unmet(
    requirements: Mapping[str, Any] | None, have: frozenset[str]
) -> tuple[list[str], list[str]]:
    """One posting: what it asks for, and which of those no résumé lists.

    Required before preferred, each alphabetical, as labels.
    """
    demands = _demands(requirements or {})
    asked = sorted(demands, key=lambda k: (demands[k] != "required", skill_label(k).lower()))
    return (
        [skill_label(k) for k in asked],
        [skill_label(k) for k in asked if k not in have],
    )


def _demands(requirements: Mapping[str, Any]) -> dict[str, str]:
    """Skill key to `required` or `preferred`, the stronger winning."""
    skills = requirements.get("skills") or {}
    strongest: dict[str, str] = {}
    for kind in ("preferred", "required"):  # required last, so it overwrites
        for entry in skills.get(kind) or []:
            if key := entry.get("skill"):
                strongest[key] = kind
    return strongest


def gap_report(resumes: Sequence[ResumeSkills], postings: Sequence[TargetPosting]) -> GapReport:
    required: Counter[str] = Counter()
    preferred: Counter[str] = Counter()
    example: dict[str, str] = {}
    read = unread = 0
    for posting in postings:
        if posting.requirements is None:
            unread += 1
            continue
        read += 1
        for key, kind in _demands(posting.requirements).items():
            (required if kind == "required" else preferred)[key] += 1
            example.setdefault(key, posting.title)

    demands = sorted(
        (
            SkillDemand(
                skill=key,
                label=skill_label(key),
                required=required[key],
                preferred=preferred[key],
                on_resumes=tuple(r.label for r in resumes if key in r.skills),
                example=example[key],
            )
            for key in required.keys() | preferred.keys()
        ),
        key=lambda d: (-d.required, -d.preferred, d.label.lower()),
    )
    return GapReport(
        resumes=tuple(resumes),
        read=read,
        unread=unread,
        missing=tuple(d for d in demands if not d.on_resumes),
        covered=tuple(d for d in demands if d.on_resumes),
    )


def _resume_text(resume: Resume) -> str:
    return "\n".join((resume.parsed_json or {}).get("raw_lines") or [])


async def target_gaps(session: AsyncSession, *, limit: int = TARGET_POSTINGS) -> GapReport:
    """The gap between the owner's base résumés and their top open matches.

    A posting matched under several profiles counts once, at its best score;
    one the owner skipped is not a target.
    """
    base = (
        await session.scalars(
            select(Resume).where(Resume.tailored_for_posting_id.is_(None)).order_by(Resume.version)
        )
    ).all()
    resumes = [
        ResumeSkills(
            label=f"v{r.version}" + (" (default)" if r.is_default else ""),
            skills=resume_skills(_resume_text(r)),
        )
        for r in base
    ]

    best = (
        select(Match.posting_id, func.max(Match.score).label("score"))
        .join(Posting, Posting.id == Match.posting_id)
        .where(
            Posting.closed_at.is_(None),
            or_(Match.decision.is_(None), Match.decision != "skipped"),
        )
        .group_by(Match.posting_id)
        .order_by(func.max(Match.score).desc())
        .limit(limit)
        .subquery()
    )
    rows = (
        await session.execute(
            select(Posting.title, Posting.requirements_json)
            .join(best, best.c.posting_id == Posting.id)
            .order_by(best.c.score.desc(), Posting.id)
        )
    ).all()
    return gap_report(
        resumes,
        [TargetPosting(title or "(untitled)", requirements) for title, requirements in rows],
    )
