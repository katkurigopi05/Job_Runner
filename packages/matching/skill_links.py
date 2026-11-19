"""The skill graph, saved: postings, projects and base résumés linked to skills.

`graph_rag`-style questions ("which jobs match my projects", "which gaps
recur") used to rebuild these links in memory for a sample of 100 postings,
in 5-7 s. Saved, they cover every posting and a question is one join.

Each link is exactly what the in-memory readers say, so a reader may switch
to the tables without changing an answer:

- **posting → skill**: `fresh_requirements` read through `skill_evidence`,
  one row per skill at its strongest reading, with the quote and whether it
  was offered as one choice among several. A stale stored reading is re-read
  in memory, never written back to the posting (`extract_requirements` does
  that). A posting with no text gets a read row marked unreadable, because
  "not read" is not "asks for nothing".
- **project → skill**: the primary language and the dependency declarations.
  A name, description or topic never makes one, and a fork makes none.
  Private and excluded projects are linked; readers decide whether they count.
- **résumé → skill**: `resume_skills` over a base résumé's lines. Tailored
  résumés are not linked.

When they are written: the crawler's store step links what it inserts or
edits (the description backfill goes through it too), GitHub sync relinks the
owner's projects, and uploading or saving a base résumé links it. `make
skill-links` relinks anything stale, which includes every posting after an
extractor version bump.

A refresh runs beside the crawler, so its writes take `FOR SHARE SKIP
LOCKED` on the postings and skip any whose text changed after it was read:
the crawl that changed it has already written the newer links.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, and_, delete, func, or_, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Candidate, Company, Posting, Project, Resume
from packages.core.models_skill_links import (
    PostingSkill,
    PostingSkillRead,
    ProjectSkill,
    ResumeSkill,
)
from packages.matching.evidence import Strength, skill_evidence
from packages.matching.gaps import resume_skills
from packages.matching.required_skills import fresh_requirements
from packages.matching.requirements import EXTRACTOR_VERSION
from packages.matching.skill_vocab import BY_KEY, normalize_skill

#: The strengths a posting link may hold, strongest first.
STRENGTHS = (Strength.REQUIRED.value, Strength.PREFERRED.value, Strength.NAMED.value)
#: The lists `requirements.py` stores skills under.
_STORED_LISTS = ("required", "preferred", "unclassified")
#: The most parameters asyncpg binds in one statement.
PARAMETER_LIMIT = 32767
#: Postings a refresh reads per batch: about 9 s when every one needs re-reading.
REFRESH_BATCH = 200


def _batches[T](items: Sequence[T], size: int) -> Iterable[Sequence[T]]:
    for first in range(0, len(items), size):
        yield items[first : first + size]


# --- postings ---------------------------------------------------------------


@dataclass(frozen=True)
class Link:
    skill: str
    strength: str
    is_option: bool
    quote: str


@dataclass(frozen=True)
class Reading:
    """One posting's links, and the text version they were read from."""

    posting_id: uuid.UUID
    content_hash: str | None
    readable: bool
    links: tuple[Link, ...]


@dataclass(frozen=True)
class LinkReport:
    linked: int = 0
    #: Read, then left alone because the posting changed or was locked.
    skipped: int = 0


def posting_links(requirements: Mapping[str, Any] | None) -> tuple[Link, ...]:
    """Each vocabulary skill a reading names, once, at its strongest."""
    if requirements is None:
        return ()
    stored = requirements.get("skills") or {}
    keys = sorted(
        {
            skill
            for name in _STORED_LISTS
            for entry in stored.get(name) or []
            if (skill := entry.get("skill")) in BY_KEY
        }
    )
    links = []
    for key in keys:
        evidence = skill_evidence(requirements, key)
        if evidence is None or evidence.strength is Strength.UNREAD:
            continue
        links.append(Link(key, evidence.strength.value, evidence.option, evidence.quote or ""))
    return tuple(links)


async def read_postings(session: AsyncSession, posting_ids: Sequence[uuid.UUID]) -> list[Reading]:
    """Read these postings' links without writing anything."""
    readings: list[Reading] = []
    for ids in _batches(list(posting_ids), PARAMETER_LIMIT):
        rows = await session.execute(
            select(
                Posting.id,
                Posting.content_hash,
                Posting.requirements_json,
                Posting.requirements_version,
                Posting.description_raw,
            ).where(Posting.id.in_(ids))
        )
        for row in rows:
            requirements = fresh_requirements(
                row.requirements_json, row.requirements_version, row.description_raw
            )
            # The crawler stores an empty current reading for a listing with no
            # text, which `fresh_requirements` returns as read. It is unknown.
            has_text = bool(row.description_raw and row.description_raw.strip())
            readings.append(
                Reading(
                    row.id,
                    row.content_hash,
                    has_text and requirements is not None,
                    posting_links(requirements),
                )
            )
    return readings


async def _unchanged(session: AsyncSession, readings: Sequence[Reading]) -> list[Reading]:
    """The readings whose posting is unlocked and still holds the text they read."""
    current: dict[uuid.UUID, str | None] = {}
    for ids in _batches([r.posting_id for r in readings], PARAMETER_LIMIT):
        rows = await session.execute(
            select(Posting.id, Posting.content_hash)
            .where(Posting.id.in_(ids))
            .order_by(Posting.id)
            .with_for_update(read=True, skip_locked=True)
        )
        current.update({row.id: row.content_hash for row in rows})
    return [
        r for r in readings if r.posting_id in current and current[r.posting_id] == r.content_hash
    ]


async def write_links(
    session: AsyncSession, readings: Sequence[Reading], *, guarded: bool = False
) -> LinkReport:
    """Replace these postings' links and read rows. Does not commit.

    `guarded` is for a writer that does not hold the postings, a refresh: it
    skips a posting another transaction holds or whose text has changed.
    """
    if not readings:
        return LinkReport()
    # In posting order, so two writers over the same postings take their
    # index locks in the same order and cannot deadlock.
    ordered = sorted(readings, key=lambda r: r.posting_id)
    kept = await _unchanged(session, ordered) if guarded else ordered
    skipped = len(readings) - len(kept)
    if not kept:
        return LinkReport(0, skipped)
    ids = [r.posting_id for r in kept]
    for some in _batches(ids, PARAMETER_LIMIT):
        await session.execute(delete(PostingSkill).where(PostingSkill.posting_id.in_(some)))
    rows = [
        {
            "posting_id": r.posting_id,
            "skill": link.skill,
            "strength": link.strength,
            "is_option": link.is_option,
            "quote": link.quote,
        }
        for r in kept
        for link in r.links
    ]
    for link_rows in _batches(rows, PARAMETER_LIMIT // 5):
        insert = pg_insert(PostingSkill).values(list(link_rows))
        await session.execute(
            insert.on_conflict_do_update(
                index_elements=["posting_id", "skill"],
                set_={c: insert.excluded[c] for c in ("strength", "is_option", "quote")},
            )
        )
    reads = [
        {
            "posting_id": r.posting_id,
            "extractor_version": EXTRACTOR_VERSION,
            "content_hash": r.content_hash,
            "readable": r.readable,
        }
        for r in kept
    ]
    for read_rows in _batches(reads, PARAMETER_LIMIT // 4):
        marks = pg_insert(PostingSkillRead).values(list(read_rows))
        await session.execute(
            marks.on_conflict_do_update(
                index_elements=["posting_id"],
                set_={
                    "extractor_version": marks.excluded.extractor_version,
                    "content_hash": marks.excluded.content_hash,
                    "readable": marks.excluded.readable,
                    "linked_at": func.now(),
                },
            )
        )
    return LinkReport(len(kept), skipped)


async def link_postings(session: AsyncSession, posting_ids: Sequence[uuid.UUID]) -> int:
    """Link postings this transaction already holds, as the crawler's store does."""
    if not posting_ids:
        return 0
    return (await write_links(session, await read_postings(session, posting_ids))).linked


def is_current() -> ColumnElement[bool]:
    """A posting's read row was made from its current text by today's extractor."""
    return _is_current()


def _is_current() -> ColumnElement[bool]:
    return and_(
        PostingSkillRead.extractor_version == EXTRACTOR_VERSION,
        PostingSkillRead.content_hash.is_not_distinct_from(Posting.content_hash),
    )


@dataclass(frozen=True)
class RefreshReport:
    linked: int
    skipped: int
    #: The last posting looked at, for the next batch to start after.
    last: uuid.UUID | None


async def refresh_postings(
    session: AsyncSession, *, after: uuid.UUID | None = None, limit: int = REFRESH_BATCH
) -> RefreshReport:
    """Relink one batch of postings never linked or linked from other text. Does not commit."""
    stale = (
        select(Posting.id)
        .outerjoin(PostingSkillRead, PostingSkillRead.posting_id == Posting.id)
        .where(or_(PostingSkillRead.posting_id.is_(None), ~_is_current()))
    )
    if after is not None:
        stale = stale.where(Posting.id > after)
    ids = list((await session.scalars(stale.order_by(Posting.id).limit(limit))).all())
    if not ids:
        return RefreshReport(0, 0, None)
    report = await write_links(session, await read_postings(session, ids), guarded=True)
    return RefreshReport(report.linked, report.skipped, ids[-1])


@dataclass(frozen=True)
class Coverage:
    open: int
    #: Open postings whose links were read from their current text by today's extractor.
    linked: int
    #: Of those, postings with no text to read.
    unreadable: int

    @property
    def stale(self) -> int:
        return self.open - self.linked


async def coverage(session: AsyncSession) -> Coverage:
    current = _is_current()
    row = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(current),
                func.count().filter(and_(current, PostingSkillRead.readable.is_(False))),
            )
            .select_from(Posting)
            .outerjoin(PostingSkillRead, PostingSkillRead.posting_id == Posting.id)
            .where(Posting.closed_at.is_(None))
        )
    ).one()
    return Coverage(open=row[0], linked=row[1], unreadable=row[2])


# --- projects and résumés ----------------------------------------------------


def project_links(project: Project) -> tuple[tuple[str, str, str], ...]:
    """(skill, origin, name as declared) for what a project's source declares."""
    if project.is_fork:
        return ()
    named = [("language", project.language)] if project.language else []
    named += [("dependency", name) for name in project.dependency_technologies or []]
    found: dict[tuple[str, str], str] = {}
    for origin, name in named:
        key = normalize_skill(name)
        if key is not None:
            found.setdefault((key, origin), name)
    return tuple((key, origin, name) for (key, origin), name in sorted(found.items()))


async def link_projects(session: AsyncSession, candidate_id: object) -> int:
    """Relink every project of one candidate. Does not commit."""
    projects = (
        await session.scalars(select(Project).where(Project.candidate_id == candidate_id))
    ).all()
    if not projects:
        return 0
    await session.execute(
        delete(ProjectSkill).where(ProjectSkill.project_id.in_([p.id for p in projects]))
    )
    rows = [
        {"project_id": p.id, "skill": key, "origin": origin, "declared_as": name[:200]}
        for p in projects
        for key, origin, name in project_links(p)
    ]
    if rows:
        await session.execute(pg_insert(ProjectSkill).values(rows).on_conflict_do_nothing())
    return len(rows)


def is_base(resume: Resume) -> bool:
    return resume.tailored_for_posting_id is None and resume.tailored_key is None


async def link_resume(session: AsyncSession, resume: Resume) -> int:
    """Link a base résumé's skills; a tailored one gets none. Does not commit."""
    if not is_base(resume):
        return 0
    text = "\n".join((resume.parsed_json or {}).get("raw_lines") or [])
    keys = sorted(resume_skills(text))
    await session.execute(delete(ResumeSkill).where(ResumeSkill.resume_id == resume.id))
    if keys:
        await session.execute(
            pg_insert(ResumeSkill)
            .values([{"resume_id": resume.id, "skill": key} for key in keys])
            .on_conflict_do_nothing()
        )
    return len(keys)


async def refresh_owner(session: AsyncSession) -> tuple[int, int]:
    """Relink every project and base résumé: (project links, résumé links)."""
    projects = 0
    for candidate_id in (await session.scalars(select(Candidate.id))).all():
        projects += await link_projects(session, candidate_id)
    resumes = 0
    base = select(Resume).where(
        Resume.tailored_for_posting_id.is_(None), Resume.tailored_key.is_(None)
    )
    for resume in (await session.scalars(base)).all():
        resumes += await link_resume(session, resume)
    return projects, resumes


# --- reading -----------------------------------------------------------------


def is_direct_requirement() -> ColumnElement[bool]:
    """Required, not one choice among several, and quoted: what `graph_rag` counts."""
    return and_(
        PostingSkill.strength == Strength.REQUIRED.value,
        PostingSkill.is_option.is_(False),
        PostingSkill.quote != "",
    )


@dataclass(frozen=True)
class OverlapJob:
    posting_id: uuid.UUID
    title: str | None
    company: str | None
    skills: tuple[str, ...]


@dataclass(frozen=True)
class Overlap:
    jobs: tuple[OverlapJob, ...]
    #: Open postings sharing at least one directly required skill.
    total: int
    seconds: float


async def project_job_overlap(
    session: AsyncSession, candidate_id: uuid.UUID, *, limit: int = 10
) -> Overlap:
    """Open jobs that directly require a technology the owner's projects declare.

    Over every linked open posting, before the search area: a caller showing
    these to the owner applies `locality.area_exclusion` as the feed does.
    Most shared skills first, then newest.
    """
    started = time.perf_counter()
    declared = (
        select(ProjectSkill.skill)
        .join(Project, Project.id == ProjectSkill.project_id)
        .where(
            Project.candidate_id == candidate_id,
            Project.is_private.is_(False),
            or_(Project.include.is_(None), Project.include.is_(True)),
        )
        .distinct()
        .subquery()
    )
    shared = (
        select(
            PostingSkill.posting_id,
            func.array_agg(aggregate_order_by(PostingSkill.skill, PostingSkill.skill)).label(
                "skills"
            ),
            func.count().label("n"),
        )
        .join(declared, declared.c.skill == PostingSkill.skill)
        .join(Posting, Posting.id == PostingSkill.posting_id)
        .join(PostingSkillRead, PostingSkillRead.posting_id == Posting.id)
        .where(Posting.closed_at.is_(None), _is_current(), is_direct_requirement())
        .group_by(PostingSkill.posting_id)
        .subquery()
    )
    total = await session.scalar(select(func.count()).select_from(shared)) or 0
    rows = await session.execute(
        select(shared.c.posting_id, Posting.title, Company.name, shared.c.skills)
        .join(Posting, Posting.id == shared.c.posting_id)
        .outerjoin(Company, Company.id == Posting.company_id)
        .order_by(shared.c.n.desc(), Posting.first_seen_at.desc(), Posting.id)
        .limit(limit)
    )
    jobs = tuple(OverlapJob(row.posting_id, row.title, row.name, tuple(row.skills)) for row in rows)
    return Overlap(jobs=jobs, total=total, seconds=time.perf_counter() - started)


__all__ = [
    "STRENGTHS",
    "Coverage",
    "Link",
    "LinkReport",
    "Overlap",
    "OverlapJob",
    "Reading",
    "RefreshReport",
    "coverage",
    "is_base",
    "is_current",
    "is_direct_requirement",
    "link_postings",
    "link_projects",
    "link_resume",
    "posting_links",
    "project_job_overlap",
    "project_links",
    "read_postings",
    "refresh_owner",
    "refresh_postings",
    "write_links",
]
