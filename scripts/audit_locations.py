"""How the search area reads every open posting's location — `make audit-locations`.

Read-only. The area is decided by hand-written vocabularies (`locality.py`),
and a vocabulary has gaps: a city it has never heard of, a new way a board
writes "United States". A gap does not raise. It quietly keeps a foreign job
or drops a local one, and the feed looks the same either way.

So this prints where gaps would show. `UNPLACED` is the list of places no rule
recognized, which are kept; a foreign city there is a missing entry. The two
"look twice" lists are the strings most likely to be misread: kept but naming
a foreign place, and dropped as abroad while naming an American one. Run it
after a crawl that added boards, and add what it finds to `locality.py` with
a test in `tests/test_location_area.py`.
"""

from __future__ import annotations

import asyncio
from collections import Counter

from sqlalchemy import select

from packages.core import db as core_db
from packages.core.config import get_settings
from packages.core.models import Posting
from packages.matching.locality import (
    Locality,
    area_exclusion,
    locality_of,
    locations_in,
    names_us_region,
)

#: Strings shown per list. The point is to be read, not exhaustive.
SHOWN = 25


def _top(counter: Counter[str], heading: str) -> None:
    total = sum(counter.values())
    print(f"\n{heading}: {len(counter)} strings, {total} postings")
    for location, count in counter.most_common(SHOWN):
        print(f"  {count:5}  {location!r}")


async def run() -> int:
    settings = get_settings()
    async with core_db.get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(Posting.location, Posting.title, Posting.description_raw).where(
                    Posting.closed_at.is_(None)
                )
            )
        ).all()

    classes: Counter[str] = Counter()
    kept = 0
    unplaced: Counter[str] = Counter()
    kept_but_foreign: Counter[str] = Counter()
    dropped_but_american: Counter[str] = Counter()
    for location, title, description in rows:
        where = locality_of(location)
        classes[where.name] += 1
        reason = area_exclusion(
            location,
            title=title,
            description=description,
            remote_outside_california=settings.search_remote_outside_california,
        )
        parts = [locality_of(part) for part in locations_in(location)]
        if reason is None:
            kept += 1
            if Locality.ELSEWHERE in parts:
                kept_but_foreign[location or ""] += 1
        elif "outside the United States" in reason and names_us_region(location):
            dropped_but_american[location or ""] += 1
        if where is Locality.UNPLACED:
            unplaced[location or ""] += 1

    print(f"{len(rows)} open postings; {kept} in the search area, {len(rows) - kept} outside it")
    print("by class: " + ", ".join(f"{name} {count}" for name, count in classes.most_common()))
    _top(unplaced, "UNPLACED, kept: a foreign place here is a missing entry")
    _top(kept_but_foreign, "Look twice, kept though one listed office is abroad")
    _top(dropped_but_american, "Look twice, dropped as abroad though it names an American place")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
