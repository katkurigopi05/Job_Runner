"""Counts and shares of open postings, computed in code.

The assistant's model may repeat a number and derive none (CLAUDE.md §14,
§27), and a caller over MCP is a model too. So the questions a model is worst
at (how many, what share, broken down how) are answered here from the rows,
with the scope in words so the answer says what was counted.

What each filter reads:

- **Role** is the role table (`roles.canonical`). It knows nine engineering
  and data roles, which on the owner's corpus on 2026-11-05 labelled 1,473 of
  19,245 open postings. **Title** is a title's role words (`roles.normalize`)
  and reads every posting with a title: "Senior Product Manager, Payments"
  is a "product manager".
- **Place** is the location reader the feed uses (`locality`), office by
  office. A title places a job only when the location field names no place,
  as `names_bay_area_office` does. California is an office in California, so
  a remote job that names no state is not in it.
- **Remote** is `locality.reads_as_remote`. Titles and locations settle most
  postings; a description is read only for one they leave open, and only
  when the answer depends on it.

Postings, not jobs: a job listed on two boards is two postings, as in the
assistant's own counts.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.config import get_settings
from packages.core.models import Company, Posting
from packages.matching.chunk_search import one_of
from packages.matching.locality import (
    area_exclusion,
    names_bay_area_office,
    names_california_office,
    names_city_office,
    reads_as_remote,
    remote_by_metadata,
)
from packages.matching.roles import ROLE_ALIASES, canonical, display_name, normalize
from packages.matching.stats_schema import (
    GroupBy,
    Place,
    PostingStats,
    StatsGroup,
    StatsRequest,
    WorkMode,
)

#: Descriptions are read this many at a time, so a count over every posting
#: whose work mode only its body states never holds all their text at once.
DESCRIPTION_BATCH = 1_000

#: The one search-area verdict a description can change, as in
#: `retrieve._open_area_total`: prose can make an on-site job remote.
_PROSE_CAN_KEEP = " is on-site outside California"
_MODE_WORDS = {
    WorkMode.ANY: "",
    WorkMode.REMOTE: "remote",
    WorkMode.NOT_REMOTE: "on-site or hybrid",
}


@dataclass(frozen=True)
class _Row:
    id: uuid.UUID
    title: str | None
    location: str | None
    company: str | None


@lru_cache(maxsize=50_000)
def _role_of(title: str) -> str | None:
    return canonical(title)


@lru_cache(maxsize=50_000)
def _words_of(title: str) -> str:
    return normalize(title)


def _kind_matches(roles: tuple[str, ...], title_words: str | None, row: _Row) -> bool:
    title = row.title or ""
    if roles and _role_of(title) not in roles:
        return False
    return not title_words or f" {title_words} " in f" {_words_of(title)} "


def _mode_matches(
    mode: WorkMode, row: _Row, description: str | None, *, final: bool
) -> bool | None:
    if mode is WorkMode.ANY:
        return True
    remote = remote_by_metadata(title=row.title, location=row.location)
    if remote is None:
        if not final:
            return None
        remote = reads_as_remote(title=row.title, location=row.location, description=description)
    return remote if mode is WorkMode.REMOTE else not remote


def _both(left: bool | None, right: bool | None) -> bool | None:
    if left is False or right is False:
        return False
    return None if left is None or right is None else True


class _Classifier:
    """Whether a posting is counted, and in the share, from what is read of it so far."""

    def __init__(self, request: StatsRequest) -> None:
        settings = get_settings()
        self.request = request
        self.us_only = settings.search_us_only
        self.remote_outside = settings.search_remote_outside_california

    def verdict(
        self, row: _Row, description: str | None, *, final: bool
    ) -> tuple[bool, bool] | None:
        """(counted, in the share), or None when only the description can say."""
        request = self.request
        if not _kind_matches(request.roles, request.title, row):
            return (False, False)
        within = _both(
            self._in_place(row, description, final=final),
            _mode_matches(request.work_mode, row, description, final=final),
        )
        if within is False:
            return (False, False)
        shared: bool | None = False
        if request.asks_share:
            shared = _kind_matches(request.share_roles, request.share_title, row) and (
                _mode_matches(request.share_work_mode, row, description, final=final)
            )
        if within is None or shared is None:
            return None
        return (True, shared)

    def _in_place(self, row: _Row, description: str | None, *, final: bool) -> bool | None:
        place = self.request.place
        if place is Place.EVERYWHERE:
            return True
        if place is Place.CALIFORNIA:
            return names_california_office(row.location, title=row.title)
        if place is Place.BAY_AREA:
            if self.request.cities:
                return names_city_office(row.location, title=row.title, cities=self.request.cities)
            return names_bay_area_office(row.location, title=row.title)
        if not self.us_only:
            return True
        reason = area_exclusion(
            row.location,
            title=row.title,
            description=description,
            remote_outside_california=self.remote_outside,
        )
        if reason is None:
            return True
        return None if not final and reason.endswith(_PROSE_CAN_KEEP) else False


async def posting_stats(session: AsyncSession, request: StatsRequest) -> PostingStats:
    """Count the open postings `request` describes, the share it asks for, and its breakdown.

    The regexes run over every open posting, and over ten thousand
    descriptions for a remote share of the search area (42 s on 2026-11-05),
    so they run in a thread, as retrieval's encoders do: the API keeps serving
    its streams meanwhile. Only the queries stay on the event loop.
    """
    classifier = _Classifier(request)
    rows = await _open_rows(session)
    counted, undecided = await asyncio.to_thread(_first_pass, classifier, rows)
    async for batch in _description_batches(session, undecided):
        counted = [*counted, *await asyncio.to_thread(_final_pass, classifier, batch)]
    return _summary(request, counted)


def _first_pass(
    classifier: _Classifier, rows: Sequence[_Row]
) -> tuple[list[tuple[_Row, bool]], list[_Row]]:
    """The postings counted, each with whether it is in the share, and those a body decides."""
    verdicts = [(row, classifier.verdict(row, None, final=False)) for row in rows]
    counted = [(row, verdict[1]) for row, verdict in verdicts if verdict is not None and verdict[0]]
    return counted, [row for row, verdict in verdicts if verdict is None]


def _final_pass(
    classifier: _Classifier, batch: Sequence[tuple[_Row, str | None]]
) -> list[tuple[_Row, bool]]:
    verdicts = [(row, classifier.verdict(row, body, final=True)) for row, body in batch]
    if any(verdict is None for _, verdict in verdicts):
        raise RuntimeError("a posting stayed undecided after its description was read")
    return [(row, verdict[1]) for row, verdict in verdicts if verdict is not None and verdict[0]]


async def _open_rows(session: AsyncSession) -> list[_Row]:
    rows = await session.execute(
        select(Posting.id, Posting.title, Posting.location, Company.name)
        .outerjoin(Company, Company.id == Posting.company_id)
        .where(Posting.closed_at.is_(None))
    )
    return [
        _Row(id=ident, title=title, location=location, company=company)
        for ident, title, location, company in rows.all()
    ]


async def _description_batches(
    session: AsyncSession, rows: Sequence[_Row]
) -> AsyncIterator[list[tuple[_Row, str | None]]]:
    for start in range(0, len(rows), DESCRIPTION_BATCH):
        batch = rows[start : start + DESCRIPTION_BATCH]
        result = await session.execute(
            select(Posting.id, Posting.description_raw).where(
                one_of(Posting.id, [row.id for row in batch])
            )
        )
        found: dict[uuid.UUID, str | None] = {
            ident: description for ident, description in result.all()
        }
        yield [(row, found.get(row.id)) for row in batch]


def _percent(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 2) if whole else None


def _summary(request: StatsRequest, counted: list[tuple[_Row, bool]]) -> PostingStats:
    total = len(counted)
    matching = sum(1 for _, shared in counted if shared) if request.asks_share else None
    groups, other, unclassified = _groups(request, [row for row, _ in counted], total)
    return PostingStats(
        scope=_scope(request),
        total=total,
        share_of=_share_words(request) if request.asks_share else None,
        matching=matching,
        percentage=_percent(matching, total) if matching is not None else None,
        group_by=request.group_by,
        groups=groups,
        other=other,
        unclassified=unclassified,
        notes=_notes(request),
    )


def _label(group_by: GroupBy, row: _Row) -> str | None:
    if group_by is GroupBy.ROLE:
        role = _role_of(row.title or "")
        return display_name(role) if role else None
    if group_by is GroupBy.TITLE:
        return _words_of(row.title or "") or None
    return row.company or None


def _groups(
    request: StatsRequest, rows: list[_Row], total: int
) -> tuple[tuple[StatsGroup, ...], int, int]:
    """The `top` groups, the postings in the groups after them, and the unlabelled."""
    if request.group_by is None:
        return (), 0, 0
    group_by = request.group_by
    labels: Counter[str | None] = Counter(_label(group_by, row) for row in rows)
    unclassified = labels.pop(None, 0)
    ranked = sorted(
        ((label, count) for label, count in labels.items() if label is not None),
        key=lambda item: (-item[1], item[0]),
    )
    shown = tuple(
        StatsGroup(label=label, count=count, percentage=_percent(count, total))
        for label, count in ranked[: request.top]
    )
    return shown, sum(count for _, count in ranked[request.top :]), unclassified


def _kind_words(roles: tuple[str, ...], title: str | None) -> str:
    words = " or ".join(display_name(role) for role in roles)
    if title:
        words = f'{words} "{title}"'.strip()
    return words


def _subject(mode: WorkMode, roles: tuple[str, ...], title: str | None) -> str:
    return " ".join(part for part in (_MODE_WORDS[mode], _kind_words(roles, title)) if part)


def _place_words(request: StatsRequest) -> str:
    if request.place is Place.EVERYWHERE:
        return "everywhere"
    if request.place is Place.CALIFORNIA:
        return "with an office in California"
    if request.place is Place.BAY_AREA:
        if request.cities:
            return "with an office in " + " or ".join(city.title() for city in request.cities)
        return "with an office in the Bay Area"
    settings = get_settings()
    if not settings.search_us_only:
        area = "no location limit"
    elif settings.search_remote_outside_california:
        area = "the United States, and outside California remote only"
    else:
        area = "the United States"
    return f"in your search area ({area})"


def _scope(request: StatsRequest) -> str:
    subject = _subject(request.work_mode, request.roles, request.title) or "open"
    return f"{subject} postings {_place_words(request)}"


def _share_words(request: StatsRequest) -> str:
    return _subject(request.share_work_mode, request.share_roles, request.share_title)


def _notes(request: StatsRequest) -> tuple[str, ...]:
    notes = ["Counts are open postings: a job listed on two boards counts twice."]
    if request.group_by is GroupBy.ROLE:
        known = ", ".join(display_name(role) for role in sorted(ROLE_ALIASES))
        notes.append(
            f"Roles are the role table's {len(ROLE_ALIASES)} ({known}). "
            "A title it does not know is counted as unclassified, not guessed."
        )
    if request.place in (Place.CALIFORNIA, Place.BAY_AREA):
        notes.append("A remote posting that names no office there is not counted as there.")
    if request.work_mode is not WorkMode.ANY or request.share_work_mode is not WorkMode.ANY:
        notes.append(
            "Remote is what the title or location says, or else a description "
            "that says this role is remote."
        )
    return tuple(notes)
