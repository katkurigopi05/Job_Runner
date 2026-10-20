"""The noted companies, and the owner's permission to look into them.

A worker notes a company it cannot read and moves on (`crawler/issues.py`).
The deep dive into the noted ones starts when the owner says so — they asked
for it at the end, with their permission (2026-10-19) — unless
`DEEP_DIVE_AUTO` gives standing permission.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from apps.api.deps import SessionDep
from packages.core.config import get_settings
from packages.crawler import issues

router = APIRouter(prefix="/company-issues", tags=["company-issues"])


class IssueSummaryOut(BaseModel):
    noted: int
    queued: int
    needs_owner: int
    fixed: int
    #: Notes a deep dive would be started for now.
    waiting_for_permission: int
    #: Standing permission: the crawl tick starts them without asking.
    deep_dive_auto: bool


class DeepDiveStartedOut(BaseModel):
    started: int


@router.get("/summary", response_model=IssueSummaryOut)
async def summary(session: SessionDep) -> IssueSummaryOut:
    counts = await issues.summary(session)
    return IssueSummaryOut(
        noted=counts.noted,
        queued=counts.queued,
        needs_owner=counts.needs_owner,
        fixed=counts.fixed,
        waiting_for_permission=counts.waiting_for_permission,
        deep_dive_auto=get_settings().deep_dive_auto,
    )


@router.post("/deep-dive", response_model=DeepDiveStartedOut)
async def start_deep_dives(
    session: SessionDep, limit: int | None = Query(default=None, ge=1, le=100_000)
) -> DeepDiveStartedOut:
    """Start a deep dive for each noted company, oldest first, or the first `limit`.

    They are claimed after every other kind of task, so they never hold up a
    crawl or a discovery.
    """
    started = await issues.start_deep_dives(session, limit=limit)
    await session.commit()
    return DeepDiveStartedOut(started=len(started))
