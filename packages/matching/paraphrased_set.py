"""The 500 paraphrased questions, asked through the real search and scored.

`docs/evaluations/paraphrased_questions_500.md` holds one question for each
of 500 open postings, written from the posting without its title, its
employer's name or any technology. This reads that file (not a copy that
could drift from it), asks each question through `retrieve()` and records
where the posting it was written from lands: among the five shown, then in
the list under them.

What the numbers are not, as `known_target` says for its own set:

- **Not relevance.** One target a question; five other postings that answer
  it as well count as a miss. 24 targets have a near-twin from the same
  employer, scored apart ("with twins"), never folded into the strict count.
- **Not a fixed set.** Postings close. A closed target, and one the search
  area now leaves out, is counted apart and never as a miss.
- **Traps are not the 500.** Appendix A keeps the 15 first wordings the
  company reader misread. They are asked as their own row.

No model is asked anything except the local query embedder in a Qwen mode,
and nothing is written.
"""

from __future__ import annotations

import re
import statistics
import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Posting
from packages.matching import retrieve as search

DEFAULT_SET = Path("docs/evaluations/paraphrased_questions_500.md")

#: Where in the list a target still counts as found for each column.
SHOWN = 5
NEAR_TOP = 20

ALL = "all"
TRAPS = "traps (original wording)"

_SECTION = re.compile(r"^## (.+?)(?: \(\d+\))?\s*$")
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")
_TARGET = re.compile(r"^\[(?P<target>.*)\]\((?P<url>https?://[^)\s]+)\)$")
_POSTING_ID = re.compile(r"^`(?P<id>[0-9a-f-]{36})`$")
_TWIN = re.compile(r"#(\d+)")


@dataclass(frozen=True)
class Case:
    number: int
    family: str
    question: str
    posting_id: uuid.UUID
    url: str
    #: "Title — Company", as the file shows it.
    target: str
    twins: tuple[int, ...]
    #: The company a trap's wording is misread as; None for the 500.
    trap: str | None = None


@dataclass(frozen=True)
class Outcome:
    number: int
    family: str
    trap: str | None
    #: The target's place in the five shown and the list under them.
    rank: int | None
    #: The best place of the target or any of its twins.
    twin_rank: int | None
    listed: int
    seconds: float
    #: The exception's type when the search raised; such a question is no miss.
    error: str | None = None


@dataclass(frozen=True)
class Row:
    group: str
    #: Questions the search answered; `errors` are the ones it raised on.
    questions: int
    errors: int
    first: int
    in_five: int
    in_twenty: int
    listed: int
    mrr: float
    in_five_twins: int
    mrr_twins: float
    median_seconds: float


@dataclass(frozen=True)
class Report:
    mode: str
    stamp: dict[str, Any]
    cases: int
    asked: int
    #: Closed or pruned since the question was written.
    gone: list[int]
    #: Open, and left out by the search area as it now stands.
    outside_area: list[int]
    outcomes: list[Outcome]
    rows: list[Row]


def _cells(line: str) -> list[str]:
    parts = _UNESCAPED_PIPE.split(line.strip())[1:-1]
    return [part.strip().replace("\\|", "|") for part in parts]


def _case(cells: list[str], family: str) -> Case:
    number, question, target_cell, _where, id_cell, twins = cells
    target = _TARGET.match(target_cell)
    posting = _POSTING_ID.match(id_cell)
    if not target or not posting:
        raise ValueError(f"question {number} has no target link or posting id")
    return Case(
        number=int(number),
        family=family,
        question=question,
        posting_id=uuid.UUID(posting["id"]),
        url=target["url"],
        target=target["target"],
        twins=tuple(int(n) for n in _TWIN.findall(twins)),
    )


def load(path: Path = DEFAULT_SET) -> tuple[list[Case], list[Case]]:
    """The questions and the traps. Raises on anything that could be misread."""
    cases: list[Case] = []
    trap_rows: list[tuple[int, str, str]] = []
    section = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        heading = _SECTION.match(line)
        if heading:
            section = heading.group(1)
            continue
        if not re.match(r"^\| \d+ \|", line):
            continue
        cells = _cells(line)
        if section.startswith("Appendix A"):
            trap_rows.append((int(cells[0]), cells[1], cells[2]))
        elif not section.startswith("Appendix"):
            cases.append(_case(cells, section))

    by_number: dict[int, Case] = {}
    for case in cases:
        if case.number in by_number:
            raise ValueError(f"question {case.number} is in the set twice")
        by_number[case.number] = case
    for case in cases:
        missing = [n for n in case.twins if n not in by_number]
        if missing:
            raise ValueError(f"question {case.number} names a twin not in the set: {missing}")
    traps = []
    for number, wording, company in trap_rows:
        if number not in by_number:
            raise ValueError(f"trap {number} has no question in the set")
        base = by_number[number]
        traps.append(Case(**{**base.__dict__, "question": wording, "trap": company}))
    return sorted(cases, key=lambda c: c.number), traps


def rank_of(listed: Sequence[uuid.UUID], target: uuid.UUID) -> int | None:
    return listed.index(target) + 1 if target in listed else None


def best_rank(listed: Sequence[uuid.UUID], targets: Iterable[uuid.UUID]) -> int | None:
    ranks = [rank for t in targets if (rank := rank_of(listed, t)) is not None]
    return min(ranks) if ranks else None


def _within(rank: int | None, depth: int) -> bool:
    return rank is not None and rank <= depth


def summarise(outcomes: Sequence[Outcome], *, group: str) -> list[Row]:
    """One row for `outcomes`, or none when there are none."""
    if not outcomes:
        return []
    failed = [o for o in outcomes if o.error is not None]
    outcomes = [o for o in outcomes if o.error is None]
    if not outcomes:
        return [Row(group, 0, len(failed), 0, 0, 0, 0, 0.0, 0, 0.0, 0.0)]
    n = len(outcomes)
    ranks = [o.rank for o in outcomes]
    twin_ranks = [o.twin_rank for o in outcomes]
    return [
        Row(
            group=group,
            questions=n,
            errors=len(failed),
            first=sum(rank == 1 for rank in ranks),
            in_five=sum(_within(rank, SHOWN) for rank in ranks),
            in_twenty=sum(_within(rank, NEAR_TOP) for rank in ranks),
            listed=sum(rank is not None for rank in ranks),
            mrr=sum(1 / rank for rank in ranks if rank) / n,
            in_five_twins=sum(_within(rank, SHOWN) for rank in twin_ranks),
            mrr_twins=sum(1 / rank for rank in twin_ranks if rank) / n,
            median_seconds=statistics.median(o.seconds for o in outcomes),
        )
    ]


def rows_for(outcomes: Sequence[Outcome]) -> list[Row]:
    """All 500 first, then each family in file order, then the traps alone."""
    main = [o for o in outcomes if o.trap is None]
    rows = summarise(main, group=ALL)
    for family in dict.fromkeys(o.family for o in main):
        rows += summarise([o for o in main if o.family == family], group=family)
    rows += summarise([o for o in outcomes if o.trap is not None], group=TRAPS)
    return rows


async def _stamp(session: AsyncSession, mode: str, switches: dict[str, Any]) -> dict[str, Any]:
    open_postings = await session.scalar(
        select(func.count()).select_from(Posting).where(Posting.closed_at.is_(None))
    )
    return {
        "measured": datetime.now(UTC).isoformat(timespec="seconds"),
        "mode": mode,
        "switches": switches,
        "open_postings": int(open_postings or 0),
    }


async def run(
    session: AsyncSession,
    cases: Sequence[Case],
    *,
    mode: str = "shipped",
    switches: dict[str, Any] | None = None,
) -> Report:
    """Ask every case whose target is open and in the area. Reads only.

    The caller sets the mode (`judged.searching_as`); `mode` and `switches`
    only label the report.
    """
    stamp = await _stamp(session, mode, switches or {})
    numbers = {c.number: c.posting_id for c in cases}
    ids = list(dict.fromkeys(c.posting_id for c in cases))
    open_ids = set(
        (
            await session.scalars(
                select(Posting.id).where(Posting.id.in_(ids), Posting.closed_at.is_(None))
            )
        ).all()
    )
    hits = await search._hits_for(session, list(open_ids))
    # The search's own rule for the area, not a copy of it.
    in_area = {pid for pid in open_ids if pid in hits and not search._outside_area(hits[pid])}

    outcomes: list[Outcome] = []
    for case in cases:
        if case.posting_id not in in_area:
            continue
        started = time.perf_counter()
        try:
            # A savepoint, so one question that raises leaves the snapshot usable.
            async with session.begin_nested():
                found = await search.retrieve(session, case.question)
        except Exception as exc:  # reported per question, never scored as a miss
            outcomes.append(
                Outcome(
                    number=case.number,
                    family=case.family,
                    trap=case.trap,
                    rank=None,
                    twin_rank=None,
                    listed=0,
                    seconds=round(time.perf_counter() - started, 3),
                    error=type(exc).__name__,
                )
            )
            continue
        seconds = time.perf_counter() - started
        listed = [p.posting_id for p in found.passages] + [e.posting_id for e in found.more]
        twins = [numbers[n] for n in case.twins if n in numbers]
        outcomes.append(
            Outcome(
                number=case.number,
                family=case.family,
                trap=case.trap,
                rank=rank_of(listed, case.posting_id),
                twin_rank=best_rank(listed, [case.posting_id, *twins]),
                listed=len(listed),
                seconds=round(seconds, 3),
            )
        )
    asked_cases = [c for c in cases if c.trap is None]
    return Report(
        mode=mode,
        stamp=stamp,
        cases=len(asked_cases),
        asked=sum(1 for o in outcomes if o.trap is None),
        gone=sorted(c.number for c in asked_cases if c.posting_id not in open_ids),
        outside_area=sorted(
            c.number
            for c in asked_cases
            if c.posting_id in open_ids and c.posting_id not in in_area
        ),
        outcomes=outcomes,
        rows=rows_for(outcomes),
    )


__all__ = [
    "ALL",
    "DEFAULT_SET",
    "TRAPS",
    "Case",
    "Outcome",
    "Report",
    "Row",
    "best_rank",
    "load",
    "rank_of",
    "rows_for",
    "run",
    "summarise",
]
