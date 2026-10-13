"""Where the one posting a question was written from lands in the search.

The measuring behind `make eval-retrieval`. Each question in
`seeds/retrieval_questions.json` was written by a model from one crawled
posting, in two forms: one that names the job and one that describes it
without its title or any technology. The real `retrieve()` answers it, and
what is recorded is the place of that posting among the five shown and the
list under them.

It existed before as scripts and question files under git-ignored `storage/`
(`docs/ML_EVALUATION.md` has every run), so the numbers could not be had
again from a checkout. This is the same measurement where a checkout has it.

What it is not, said here because every table it prints invites the reading:

- **Not relevance.** One target a question. A paraphrase describes a kind of
  job that many postings are, and five other postings that answer it count
  as a miss. `docs/RAG_PIPELINE.md` has what would measure relevance, and it
  is the owner's grades.
- **Not one number.** `dev` is the set search rules were chosen on and
  `heldout` the one they were not. They are never added, and nor are the two
  forms of question.
- **Not a fixed set.** Postings close and are pruned after 30 days (§16). A
  target that is gone is counted as gone and never as a miss, and a target
  the owner's search area leaves out is counted apart for the same reason:
  the search is not at fault for a rule it was told to keep.

No model is asked anything, and nothing is written.
"""

from __future__ import annotations

import json
import statistics
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.config import get_settings
from packages.core.models import Posting
from packages.core.models_chunks import PostingChunk
from packages.matching import retrieve as search

DEFAULT_SEED = Path("seeds/retrieval_questions.json")

#: The two forms each case is asked in, in the order they are reported.
KINDS = ("natural", "paraphrased")


@dataclass(frozen=True)
class Case:
    """One posting and the two questions written from it."""

    set: str
    #: Whether a search rule or setting was chosen by looking at this set.
    tuned_on: bool
    url: str
    title: str
    natural: str
    paraphrased: str


@dataclass(frozen=True)
class Outcome:
    set: str
    kind: str
    url: str
    #: The target's place among the five shown and the list under them.
    rank: int | None
    listed: int
    seconds: float


@dataclass(frozen=True)
class Row:
    """One set and one form of question. Never a total across either."""

    set: str
    kind: str
    questions: int
    first: int
    in_five: int
    listed: int
    mrr: float
    median_seconds: float


@dataclass(frozen=True)
class Report:
    #: What the numbers were measured on, so the next run can be compared.
    stamp: dict[str, Any]
    cases: int
    #: Cases whose posting is open and inside the search area: the ones asked.
    present: int
    #: Closed or pruned since the question was written.
    gone: int
    #: Open, and left out by the owner's search area.
    outside_area: int
    outcomes: list[Outcome]
    rows: list[Row]


def load(path: Path = DEFAULT_SEED) -> list[Case]:
    """The tracked question set. Raises on a case that could not be asked."""
    cases: list[Case] = []
    for name, block in json.loads(path.read_text(encoding="utf-8"))["sets"].items():
        for entry in block["cases"]:
            for key in ("url", *KINDS):
                if not str(entry.get(key) or "").strip():
                    raise ValueError(f"a case in {name!r} has no {key}: {entry.get('url')!r}")
            cases.append(
                Case(
                    set=name,
                    tuned_on=bool(block["tuned_on"]),
                    url=entry["url"],
                    title=str(entry.get("title") or ""),
                    natural=entry["natural"],
                    paraphrased=entry["paraphrased"],
                )
            )
    return cases


def summarise(outcomes: list[Outcome]) -> list[Row]:
    """A row for each set and form that has a question in it, in file order."""
    rows: list[Row] = []
    for name in dict.fromkeys(outcome.set for outcome in outcomes):
        for kind in KINDS:
            group = [o for o in outcomes if (o.set, o.kind) == (name, kind)]
            if not group:
                continue
            ranks = [o.rank for o in group]
            rows.append(
                Row(
                    set=name,
                    kind=kind,
                    questions=len(group),
                    first=sum(rank == 1 for rank in ranks),
                    in_five=sum(rank is not None and rank <= 5 for rank in ranks),
                    listed=sum(rank is not None for rank in ranks),
                    mrr=sum(1 / rank for rank in ranks if rank) / len(group),
                    median_seconds=statistics.median(o.seconds for o in group),
                )
            )
    return rows


async def _stamp(session: AsyncSession) -> dict[str, Any]:
    settings = get_settings()
    open_postings = await session.scalar(
        select(func.count()).select_from(Posting).where(Posting.closed_at.is_(None))
    )
    chunk_models = (await session.scalars(select(PostingChunk.embedding_model).distinct())).all()
    return {
        "measured": datetime.now(UTC).isoformat(timespec="seconds"),
        "open_postings": int(open_postings or 0),
        "chunk_models": sorted(str(model) for model in chunk_models),
        "semantic_candidates": bool(settings.chat_semantic_candidates),
        "rerank_model": settings.chat_rerank_model,
        "search_area_applied": bool(settings.search_us_only),
    }


async def run(session: AsyncSession, cases: list[Case]) -> Report:
    """Ask every case whose posting can still be found. Reads only."""
    stamp = await _stamp(session)
    rows = await session.execute(
        select(Posting.url, Posting.id).where(
            Posting.url.in_([case.url for case in cases]), Posting.closed_at.is_(None)
        )
    )
    open_now: dict[str, uuid.UUID] = {url: posting_id for url, posting_id in rows.all()}
    hits = await search._hits_for(session, list(open_now.values()))
    # The search's own rule for the area, not a copy of it.
    in_area = {
        url
        for url, posting_id in open_now.items()
        if posting_id in hits and not search._outside_area(hits[posting_id])
    }

    outcomes: list[Outcome] = []
    for case in cases:
        if case.url not in in_area:
            continue
        target = str(open_now[case.url])
        for kind in KINDS:
            started = time.perf_counter()
            found = await search.retrieve(session, getattr(case, kind))
            seconds = time.perf_counter() - started
            listed = [str(passage.posting_id) for passage in found.passages]
            listed += [str(entry.posting_id) for entry in found.more]
            outcomes.append(
                Outcome(
                    set=case.set,
                    kind=kind,
                    url=case.url,
                    rank=listed.index(target) + 1 if target in listed else None,
                    listed=len(listed),
                    seconds=round(seconds, 3),
                )
            )
    return Report(
        stamp=stamp,
        cases=len(cases),
        present=len(in_area),
        gone=len(cases) - len(open_now),
        outside_area=len(open_now) - len(in_area),
        outcomes=outcomes,
        rows=summarise(outcomes),
    )


__all__ = ["DEFAULT_SEED", "KINDS", "Case", "Outcome", "Report", "Row", "load", "run", "summarise"]
