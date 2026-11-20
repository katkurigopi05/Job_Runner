"""Collapse word-for-word job listings for presentation after retrieval ranks them."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Posting

if TYPE_CHECKING:
    from packages.matching.retrieve import Listed, Passage, Retrieval


@dataclass(frozen=True)
class ListingIdentity:
    posting_id: uuid.UUID
    company_id: uuid.UUID | None
    title: str | None
    digest: str | None
    newest: datetime
    location: str | None


@dataclass(frozen=True)
class ListingGroup:
    representative: uuid.UUID
    members: tuple[uuid.UUID, ...]
    locations: tuple[str, ...]


def group_listings(rows: Sequence[ListingIdentity]) -> list[ListingGroup]:
    grouped: dict[tuple[object, ...], list[ListingIdentity]] = {}
    for row in rows:
        title = " ".join((row.title or "").casefold().split())
        key = (
            (row.company_id, title, row.digest)
            if row.company_id and title and row.digest
            else (row.posting_id,)
        )
        grouped.setdefault(key, []).append(row)
    return [
        ListingGroup(
            representative=max(group, key=lambda r: (r.newest, str(r.posting_id))).posting_id,
            members=tuple(r.posting_id for r in group),
            locations=tuple(
                dict.fromkeys(
                    r.location.strip() for r in group if r.location and r.location.strip()
                )
            ),
        )
        for group in grouped.values()
    ]


async def collapse(session: AsyncSession, found: Retrieval, question: str, limit: int) -> Retrieval:
    from packages.matching import retrieve as search
    from packages.matching import tiers

    entries: tuple[Passage | Listed, ...] = (*found.passages, *found.more)
    if not entries:
        return found
    identities = {}
    # PostgreSQL computes the digest so complete further lists never transfer
    # thousands of descriptions to Python. Normalization matches chunks.normalize:
    # whitespace only. Company identity is the FK, never the display name.
    ids = list(dict.fromkeys(e.posting_id for e in entries))
    for start in range(0, len(ids), 1000):
        rows = await session.execute(
            select(
                Posting.id,
                Posting.company_id,
                Posting.title,
                func.md5(
                    func.nullif(
                        func.btrim(func.regexp_replace(Posting.description_raw, r"\s+", " ", "g")),
                        "",
                    )
                ),
                Posting.first_seen_at,
                Posting.location,
            ).where(Posting.id.in_(ids[start : start + 1000]))
        )
        for ident, company, title, digest, newest, location in rows:
            identities[ident] = ListingIdentity(ident, company, title, digest, newest, location)
    groups = group_listings([identities[i] for i in ids if i in identities])
    by_id = {e.posting_id: e for e in entries}
    group_by_id = {g.representative: g for g in groups}
    ordered = [g.representative for g in groups]
    group_tiers = {g.representative: min(by_id[i].tier for i in g.members) for g in groups}
    tiered = any(e.tier != 0 for e in entries)
    shown, rest = (
        tiers.split(ordered, group_tiers, limit) if tiered else (ordered[:limit], ordered[limit:])
    )
    repeated = len(groups) != len(ids)
    if not repeated:
        return replace(found, job_total=len(groups), listing_total=len(ids))
    # Representative selection doesn't change a group's rank. Only the final
    # five require bodies, excerpts and application statuses to be hydrated.
    hits = await search._hits_for(session, shown)
    statuses = await search._application_statuses(session, shown)
    passages = []
    for index, ident in enumerate(shown, 1):
        group = group_by_id[ident]
        item = by_id[ident]
        source = next(
            (by_id[i] for i in group.members if getattr(by_id[i], "excerpt", None) is not None),
            None,
        )
        snippet = (
            source.excerpt
            if source is not None
            else search.excerpt(hits[ident].description or "", question)
        )
        passages.append(
            search.Passage(
                label=f"P{index}",
                posting_id=ident,
                title=item.title,
                company=item.company,
                location=item.location,
                url=item.url,
                excerpt=snippet or "",
                application_status=statuses.get(ident),
                kind=by_id[group.members[0]].kind,
                listing_count=len(group.members),
                locations=group.locations,
                tier=group_tiers[ident],
            )
        )
    more = tuple(
        search.Listed(
            posting_id=ident,
            title=by_id[ident].title,
            company=by_id[ident].company,
            location=by_id[ident].location,
            url=by_id[ident].url,
            kind=by_id[group_by_id[ident].members[0]].kind,
            listing_count=len(group_by_id[ident].members),
            locations=group_by_id[ident].locations,
            tier=group_tiers[ident],
            excerpt=getattr(by_id[ident], "excerpt", None),
        )
        for ident in rest
    )
    return replace(
        found, passages=tuple(passages), more=more, job_total=len(groups), listing_total=len(ids)
    )
