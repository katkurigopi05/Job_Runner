"""Read-only Workday inventory and one-page verification; no discovery writes.

JOBRUNNER_ENV_FILE=/path/to/.env python -m scripts.probe_workday --out /tmp/workday.json

Use while no other Workday probe is running: one shared in-memory host limiter
keeps this process polite without writing reservations or metrics to Postgres.
The ordinary crawler continues to use its shared, persistent limiter.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text

from packages.core.db import get_engine, get_sessionmaker
from packages.crawler.fetch import PoliteFetcher
from packages.crawler.find_boards import Resolved, resolve_site
from packages.crawler.workday import parse_site


async def inventory() -> list[dict[str, Any]]:
    """Every database query is inside this read-only snapshot."""
    async with get_sessionmaker()() as session:
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        await session.execute(text("SET LOCAL statement_timeout = '30s'"))
        rows = (
            (
                await session.execute(
                    text("""
            SELECT id::text, name, source_evidence->>'job_system_url' AS url
            FROM companies WHERE source_evidence->>'job_system' = 'Workday'
            ORDER BY name
        """)
                )
            )
            .mappings()
            .all()
        )
        await session.rollback()
    return [dict(row) for row in rows]


async def probe(rows: list[dict[str, Any]], out: Path) -> dict[str, Any]:
    semaphore = asyncio.Semaphore(16)
    results: list[dict[str, Any]] = []
    async with PoliteFetcher(record_metrics=False) as fetcher:

        async def one(row: dict[str, Any]) -> None:
            result = dict(row)
            parsed = parse_site(row["url"] or "")
            if parsed is None:
                result["status"] = "unparseable"
            else:
                result.update(zip(("host", "tenant", "site"), parsed, strict=True))
                async with semaphore:
                    outcome, reading = await resolve_site(
                        row["name"], fetcher, url=row["url"], guess=False
                    )
                if isinstance(outcome, Resolved):
                    result.update(status="verified", jobs=outcome.open_jobs, slug=outcome.slug)
                else:
                    result.update(status=reading.category, reason=outcome[1])
            results.append(result)
            print(f"{len(results)}/{len(rows)} {row['name']}: {result['status']}", flush=True)

        await asyncio.gather(*(one(row) for row in rows))
    report = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "read_only": True,
        "user_agent": fetcher.user_agent,
        "host_floor_seconds": 60,
        "companies": len(rows),
        "parsed": sum("tenant" in row for row in results),
        "statuses": dict(Counter(row["status"] for row in results)),
        "results": sorted(results, key=lambda row: row["name"]),
    }
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        rows = await inventory()
        report = await probe(rows, args.out)
        print(json.dumps({key: value for key, value in report.items() if key != "results"}))
    finally:
        await get_engine().dispose()


if __name__ == "__main__":
    asyncio.run(main())
