"""Read pay, skills and education out of postings stored before extraction existed.

    make extract-requirements             # every posting not at the current version
    make extract-requirements limit=500   # a bounded run

Local and CPU-only: no network, no model, no provider call. Resumable and
idempotent — each batch commits, and a posting already at
`requirements.EXTRACTOR_VERSION` is never selected again, so an interrupted run
picks up where it stopped and a finished one changes nothing.

New and edited postings are read by the crawler as they arrive; this is only
for the rows that predate that, and for rows an extractor bump invalidates.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

BATCH_SIZE = 500


async def run(*, limit: int | None = None, batch_size: int = BATCH_SIZE) -> str:
    from sqlalchemy import or_, select, update

    from packages.core.db import get_sessionmaker
    from packages.core.models import Posting
    from packages.matching.requirements import EXTRACTOR_VERSION, extract

    done = 0
    with_pay = 0
    async with get_sessionmaker()() as session:
        while limit is None or done < limit:
            size = batch_size if limit is None else min(batch_size, limit - done)
            rows = (
                await session.execute(
                    select(Posting.id, Posting.description_raw)
                    .where(
                        or_(
                            Posting.requirements_version.is_(None),
                            Posting.requirements_version < EXTRACTOR_VERSION,
                        )
                    )
                    .order_by(Posting.id)
                    .limit(size)
                )
            ).all()
            if not rows:
                break
            now = datetime.now(UTC)
            updates = []
            for posting_id, description in rows:
                reading = extract(description)
                pay = reading.compensation
                with_pay += pay is not None
                updates.append(
                    {
                        "id": posting_id,
                        "salary_min": pay.minimum if pay else None,
                        "salary_max": pay.maximum if pay else None,
                        "salary_currency": pay.currency if pay else None,
                        "salary_period": pay.period if pay else None,
                        "requirements_json": reading.as_json(),
                        "requirements_version": EXTRACTOR_VERSION,
                        "requirements_extracted_at": now,
                    }
                )
            await session.execute(update(Posting), updates)
            await session.commit()
            done += len(rows)
            print(f"  {done} postings read", flush=True)
    return f"{done} postings extracted at version {EXTRACTOR_VERSION}; {with_pay} state pay"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    print(asyncio.run(run(limit=args.limit)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
