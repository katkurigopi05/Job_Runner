"""One board is polled under one company row.

A board is an ATS and a slug: `greenhouse/vercel`. On 2026-10-06 eleven of
them were each verified under two company rows (Vercel and ZEIT, Elastic and
Elastic.co, Flexport and Deliverr), because the owner's sheet lists a company
under an old name and a new one, discovery resolved both to the same board,
and nothing asked whether somebody already held it. Each was fetched twice a
cycle and every posting on it stored twice: 282 of 11,429 open postings.

Three things here, used in three places:

- `holder_of` is what discovery asks before it verifies a company
  (`apps/worker/discover_company_job.py`).
- `set_aside` is what happens to the row that does not get the board. It stays
  in the registry as a failed row carrying what it resolved to and who holds
  it. The registry keeps evidence, not deletions (CLAUDE.md §9): a slug shared
  today may be a rename that reverses, and the row is the only record that the
  sheet named this company at all.
- `merge` repairs a database that already holds both, and is what
  `make merge-duplicate-boards` runs. After `uq_companies_verified_board`
  exists that state cannot be written again, so this is a repair for a
  database older than the index.

What `merge` will not delete is what `retention.py` will not: a posting the
owner applied to, swiped on, tailored a résumé for or graded. Such a copy is
left where it is and counted.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.enums import SourceStatus
from packages.core.models import Company, CompanyCrawlState, Match, Posting
from packages.crawler.retention import _owned

log = structlog.get_logger(__name__)

#: `source_evidence["method"]` on a row set aside for sharing a board.
DUPLICATE_METHOD = "duplicate_board"

#: How long before discovery looks at a set-aside row again. Long, because the
#: answer changes only if the holder gives the board up.
RECHECK_AFTER = timedelta(days=30)

_NOT_ALNUM = re.compile(r"[^a-z0-9]+")


def _plain(text: str | None) -> str:
    return _NOT_ALNUM.sub("", (text or "").lower())


def keeper_of(companies: Sequence[Company]) -> Company:
    """Of the rows sharing one board, the one that keeps it.

    The row named like the board, because the name is what the owner reads on
    a card: `vercel` belongs to "Vercel" rather than "ZEIT". A name equal to
    the slug first; then a name the slug contains or is contained in
    ("Teleport" in `goteleport`), nearest in length; then the older row.
    """

    def order(company: Company) -> tuple[int, int, datetime, str]:
        name, slug = _plain(company.name), _plain(company.slug)
        if name == slug:
            closeness = 0
        elif name and (name in slug or slug in name):
            closeness = 1
        else:
            closeness = 2
        created = company.created_at or datetime.max.replace(tzinfo=UTC)
        return closeness, abs(len(name) - len(slug)), created, str(company.id)

    return min(companies, key=order)


async def holder_of(
    session: AsyncSession, ats: str, slug: str, *, other_than: uuid.UUID
) -> Company | None:
    """The verified company already polling `ats`/`slug`, if it is not `other_than`."""
    return (
        await session.scalars(
            select(Company)
            .where(
                Company.ats_type == ats,
                Company.slug == slug,
                Company.source_status == SourceStatus.VERIFIED.value,
                Company.id != other_than,
            )
            .order_by(Company.created_at, Company.id)
            .limit(1)
        )
    ).first()


async def set_aside(
    session: AsyncSession,
    company: Company,
    holder: Company,
    *,
    ats: str,
    slug: str,
    now: datetime,
) -> None:
    """Record that `company` resolved to a board `holder` already has. Does not commit."""
    previous = dict(company.source_evidence or {})
    company.source_status = SourceStatus.FAILED.value
    company.slug = None
    company.source_verified_at = None
    company.discovery_failure = (
        f"same board as {holder.name}: {ats}/{slug} is already polled under that name"
    )
    company.source_evidence = {
        "method": DUPLICATE_METHOD,
        "duplicate_of": str(holder.id),
        "duplicate_of_name": holder.name,
        "ats": ats,
        "slug": slug,
        "previous": previous,
        "at": now.isoformat(),
    }
    state = await session.scalar(
        select(CompanyCrawlState).where(CompanyCrawlState.company_id == company.id)
    )
    if state is None:
        state = CompanyCrawlState(company_id=company.id)
        session.add(state)
    state.next_due_at = None
    state.discovery_next_at = now + RECHECK_AFTER
    await session.flush()


@dataclass(frozen=True)
class MergeReport:
    #: Boards that were held by more than one verified company.
    boards: int
    #: Rows that lose the board and stay in the registry as failed.
    companies_set_aside: int
    #: Copies deleted: the keeper holds the same posting.
    postings_deleted: int
    #: Postings only the other row had, moved to the keeper.
    postings_moved: int
    #: Scores only a copy had, moved to the keeper's posting.
    matches_moved: int
    #: Copies left in place because the owner did something with them.
    kept_for_owner: int
    #: "Keeper <- other, other" for each board, for the command's output.
    pairs: tuple[str, ...] = ()

    def summary(self) -> str:
        return (
            f"{self.boards} boards held twice: {self.companies_set_aside} rows set aside, "
            f"{self.postings_deleted} copies deleted, {self.postings_moved} postings moved, "
            f"{self.matches_moved} scores moved, {self.kept_for_owner} kept for the owner"
        )


async def duplicated(session: AsyncSession) -> list[list[Company]]:
    """Verified companies grouped by board, for the boards held more than once."""
    rows = (
        await session.scalars(
            select(Company)
            .where(
                Company.source_status == SourceStatus.VERIFIED.value,
                Company.slug.is_not(None),
                Company.ats_type.is_not(None),
            )
            .order_by(Company.ats_type, Company.slug, Company.created_at, Company.id)
        )
    ).all()
    by_board: dict[tuple[str, str], list[Company]] = defaultdict(list)
    for company in rows:
        by_board[(str(company.ats_type), str(company.slug))].append(company)
    return [group for group in by_board.values() if len(group) > 1]


async def _fold(
    session: AsyncSession, keeper: Company, other: Company, *, apply: bool
) -> tuple[int, int, int, int]:
    """Fold `other`'s postings into `keeper`'s: deleted, moved, scores moved, kept."""
    held = {
        external_id: posting_id
        for external_id, posting_id in (
            await session.execute(
                select(Posting.external_id, Posting.id).where(Posting.company_id == keeper.id)
            )
        ).all()
    }
    copies = (await session.scalars(select(Posting).where(Posting.company_id == other.id))).all()
    deleted = moved = scores = kept = 0
    for copy in copies:
        original = held.get(copy.external_id)
        if original is None:
            moved += 1
            if apply:
                copy.company_id = keeper.id
            continue
        if await session.scalar(select(exists().where(Posting.id == copy.id, _owned()))):
            kept += 1
            continue
        scored = set(
            (
                await session.scalars(select(Match.profile_id).where(Match.posting_id == original))
            ).all()
        )
        for match in (
            await session.scalars(select(Match).where(Match.posting_id == copy.id))
        ).all():
            if match.profile_id not in scored:
                scores += 1
                if apply:
                    match.posting_id = original
        deleted += 1
        if apply:
            # The moved scores first, or the cascade takes them with the copy.
            await session.flush()
            await session.execute(delete(Posting).where(Posting.id == copy.id))
            session.expunge(copy)
    if apply:
        await session.flush()
    return deleted, moved, scores, kept


async def absorb(
    session: AsyncSession, keeper: Company, other: Company, *, ats: str, slug: str, now: datetime
) -> tuple[int, int, int, int]:
    """`keeper` takes `ats`/`slug` from `other`, postings included. Does not commit.

    `other` is set aside first, because the index allows one verified row for
    the board and `keeper` may not be written yet. Then its postings are
    folded in. They cannot be left behind: nothing polls a set-aside row, so
    nothing would ever close them, and the keeper's next fetch would store
    each one a second time. Returns deleted, moved, scores moved, kept.
    """
    await set_aside(session, other, keeper, ats=ats, slug=slug, now=now)
    session.add(keeper)
    await session.flush()
    return await _fold(session, keeper, other, apply=True)


async def merge(session: AsyncSession, *, now: datetime, apply: bool) -> MergeReport:
    """Leave each board with one verified company and one copy of each posting.

    Does not commit. With `apply` false nothing is written and the report says
    what would be.
    """
    groups = await duplicated(session)
    set_aside_count = deleted = moved = scores = kept = 0
    pairs: list[str] = []
    for group in groups:
        keeper = keeper_of(group)
        others = [company for company in group if company.id != keeper.id]
        pairs.append(f"{keeper.name} <- {', '.join(other.name for other in others)}")
        for other in others:
            if apply:
                ats, slug = str(keeper.ats_type), str(keeper.slug)
                d, m, s, k = await absorb(session, keeper, other, ats=ats, slug=slug, now=now)
            else:
                d, m, s, k = await _fold(session, keeper, other, apply=False)
            deleted, moved, scores, kept = deleted + d, moved + m, scores + s, kept + k
            set_aside_count += 1
    report = MergeReport(
        boards=len(groups),
        companies_set_aside=set_aside_count,
        postings_deleted=deleted,
        postings_moved=moved,
        matches_moved=scores,
        kept_for_owner=kept,
        pairs=tuple(pairs),
    )
    log.info("duplicate_boards_merged", applied=apply, summary=report.summary())
    return report
