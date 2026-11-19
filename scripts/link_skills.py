"""Fill or refresh the saved skill graph — `make skill-links`.

Links every posting never linked, or linked from other text or by another
requirements extractor, then every project and base résumé. Commits after each
batch, so a stopped run keeps what it finished, and runs safely beside the
crawler (`packages/matching/skill_links.py`). Then prints how much of the open
corpus is linked and the open jobs that directly require a technology the
owner's projects declare.
"""

from __future__ import annotations

import asyncio
import time

from sqlalchemy import select

from packages.core import db as core_db
from packages.core.models import Candidate
from packages.matching import skill_links
from packages.matching.skill_vocab import BY_KEY

SHOWN = 10


async def _postings() -> tuple[int, int]:
    started, linked, skipped, after = time.perf_counter(), 0, 0, None
    while True:
        async with core_db.get_sessionmaker()() as session:
            report = await skill_links.refresh_postings(session, after=after)
            await session.commit()
        if report.last is None:
            return linked, skipped
        linked, skipped, after = linked + report.linked, skipped + report.skipped, report.last
        rate = linked / (time.perf_counter() - started)
        print(
            f"  {linked} postings linked, {skipped} left to the crawler ({rate:.0f}/s)", flush=True
        )


async def run() -> int:
    linked, skipped = await _postings()
    async with core_db.get_sessionmaker()() as session:
        projects, resumes = await skill_links.refresh_owner(session)
        await session.commit()
        coverage = await skill_links.coverage(session)
        candidates = (await session.scalars(select(Candidate.id).limit(2))).all()
        overlap = (
            await skill_links.project_job_overlap(session, candidates[0], limit=SHOWN)
            if len(candidates) == 1
            else None
        )
    print(
        f"linked {linked} postings ({skipped} skipped: being crawled, picked up next run); "
        f"{projects} project links, {resumes} base résumé links"
    )
    print(
        f"open postings: {coverage.open}; linked from current text: {coverage.linked} "
        f"({coverage.unreadable} have no text to read); stale or unlinked: {coverage.stale}"
    )
    if overlap is None:
        print("project matches: need exactly one candidate")
        return 0
    print(
        f"{overlap.total} open postings directly require a technology your projects declare "
        f"(all postings, before the search area; {overlap.seconds * 1000:.0f} ms):"
    )
    for job in overlap.jobs:
        names = ", ".join(BY_KEY[key].label if key in BY_KEY else key for key in job.skills)
        print(f"  {job.title} — {job.company or '(company not recorded)'}: {names}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
