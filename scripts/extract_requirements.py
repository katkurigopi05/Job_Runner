"""Read pay, skills and education out of postings stored before extraction existed.

    make extract-requirements             # every posting not at the current version
    make extract-requirements limit=500   # a bounded run

Local and CPU-only: no network, no model, no provider call. Resumable and
idempotent — each batch commits, and a posting already at
`requirements.EXTRACTOR_VERSION` is never selected again, so an interrupted run
picks up where it stopped and a finished one changes nothing.

New and edited postings are read by the crawler as they arrive; this is only
for the rows that predate that, and for rows an extractor bump invalidates.
Writes require the snapshotted content hash and a still-stale version, so a
concurrent crawl wins. Skipped rows are counted and left for the next run if
still stale; each run advances through posting IDs without retrying them.
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Table, cast, column, or_, select, update, values
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from packages.core import db
from packages.core.models import Posting
from packages.matching.requirements import EXTRACTOR_VERSION, extract

BATCH_SIZE = 500


@dataclass(frozen=True)
class Snapshot:
    posting_id: uuid.UUID
    description: str | None
    content_hash: str | None


@dataclass(frozen=True)
class WriteReport:
    extracted: int
    skipped: int
    with_pay: int


def _stale() -> ColumnElement[bool]:
    return or_(
        Posting.requirements_version.is_(None),
        Posting.requirements_version < EXTRACTOR_VERSION,
    )


async def read_batch(
    session: AsyncSession, *, limit: int = BATCH_SIZE, after: uuid.UUID | None = None
) -> list[Snapshot]:
    """Snapshot one ordered batch. Does not lock rows or commit."""
    query = select(Posting.id, Posting.description_raw, Posting.content_hash).where(_stale())
    if after is not None:
        query = query.where(Posting.id > after)
    rows = await session.execute(query.order_by(Posting.id).limit(limit))
    return [Snapshot(posting_id, description, digest) for posting_id, description, digest in rows]


async def write_batch(session: AsyncSession, rows: Sequence[Snapshot]) -> WriteReport:
    """Extract and conditionally write snapshots. The caller commits the batch."""
    table = Posting.__table__
    assert isinstance(table, Table)
    written, with_pay = 0, 0
    now = datetime.now(UTC)
    fields = (
        "salary_min",
        "salary_max",
        "salary_currency",
        "salary_period",
        "requirements_json",
    )
    # Seven binds per VALUES row plus version/time/predicate: at most 3,503 per
    # statement, below asyncpg's 32,767 limit even for a larger read batch.
    for start in range(0, len(rows), BATCH_SIZE):
        payload = []
        pay_ids = set()
        for row in rows[start : start + BATCH_SIZE]:
            reading = extract(row.description)
            pay = reading.compensation
            if pay is not None:
                pay_ids.add(row.posting_id)
            payload.append(
                (
                    row.posting_id,
                    row.content_hash,
                    pay.minimum if pay else None,
                    pay.maximum if pay else None,
                    pay.currency if pay else None,
                    pay.period if pay else None,
                    reading.as_json(),
                )
            )
        readings = values(
            column("posting_id", table.c.id.type),
            column("expected_hash", table.c.content_hash.type),
            *(column(name, table.c[name].type) for name in fields),
            name="readings",
        ).data(payload)
        result = await session.execute(
            update(table)
            .where(
                table.c.id == readings.c.posting_id,
                table.c.content_hash.is_not_distinct_from(readings.c.expected_hash),
                _stale(),
            )
            .values(
                # An all-NULL VALUES column is text to PostgreSQL, even when
                # SQLAlchemy knows its type. Cast unknown pay back to its type.
                **{name: cast(readings.c[name], table.c[name].type) for name in fields},
                requirements_version=EXTRACTOR_VERSION,
                requirements_extracted_at=now,
            )
            .returning(table.c.id)
        )
        written_ids = set(result.scalars())
        written += len(written_ids)
        with_pay += len(written_ids & pay_ids)
    return WriteReport(written, len(rows) - written, with_pay)


async def run(*, limit: int | None = None, batch_size: int = BATCH_SIZE) -> str:
    scanned = done = skipped = with_pay = 0
    after = None
    async with db.get_sessionmaker()() as session:
        while limit is None or scanned < limit:
            size = batch_size if limit is None else min(batch_size, limit - scanned)
            rows = await read_batch(session, limit=size, after=after)
            if not rows:
                break
            report = await write_batch(session, rows)
            await session.commit()
            scanned += len(rows)
            after = rows[-1].posting_id
            done += report.extracted
            skipped += report.skipped
            with_pay += report.with_pay
            print(f"  {done} postings extracted; {skipped} skipped", flush=True)
    return (
        f"{done} postings extracted at version {EXTRACTOR_VERSION}; {with_pay} state pay; "
        f"{skipped} skipped (changed or already current; picked up next run if still stale)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    print(asyncio.run(run(limit=args.limit)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
