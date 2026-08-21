"""`make tailor-batch` — tailor tonight what you will send tomorrow.

    python -m scripts.batch_tailor
    python -m scripts.batch_tailor --limit 20

Tailors every posting rated `interested` on `/swipe` that has no tailored
résumé yet, highest score first, and attaches each to its match. The apply
pipeline then reuses it and never waits on a model.

Stops with a margin of calls in hand rather than discovering the day's limit
by hitting it — a half-tailored queue you cannot tell apart from a whole one
is worse than a short one.
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from packages.core.config import get_settings
from packages.llm import router as llm_router
from packages.tailor import batch


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=batch.DEFAULT_LIMIT)
    parser.add_argument("--profile-id", default=None)
    args = parser.parse_args()

    settings = get_settings()
    engine = create_async_engine(
        settings.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    )
    maker = async_sessionmaker(engine, expire_on_commit=False)

    provider = llm_router.tailor_resume()
    print(f"provider: {getattr(provider, 'name', '?')}")

    async with maker() as session:
        waiting = await batch.pending(session, args.profile_id, limit=args.limit)
        if not waiting:
            print("\nnothing to tailor — rate some postings on /swipe first")
            await engine.dispose()
            return 0
        print(f"tailoring {len(waiting)} postings you marked interested\n")

        result = await batch.run(session, provider, profile_id=args.profile_id, limit=args.limit)

    for title, note in result.per_posting:
        print(f"  {title[:58]:60} {note}")
    print(f"\n  {result.summary()}")

    await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
