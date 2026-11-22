"""Chunk every open posting for search — `make chunk-postings`.

The matching pass after each crawl chunks new postings 500 at a time; this
clears a backlog in one go, committing after each batch so an interrupted run
keeps what it finished. It cuts offsets only: the chunks' Qwen vectors come
from `make export-chunk-vectors-pending` and `make import-chunk-vectors`.
"""

from __future__ import annotations

import asyncio
import time

from sqlalchemy import func, select

from packages.core import db as core_db
from packages.core.models_chunks import PostingChunk
from packages.matching.chunks import chunk_postings


async def run() -> int:
    started, total = time.perf_counter(), 0
    while True:
        async with core_db.get_sessionmaker()() as session:
            done = await chunk_postings(session)
            await session.commit()
        if not done:
            break
        total += done
        rate = total / (time.perf_counter() - started)
        print(f"  {total} postings chunked ({rate:.1f}/s)", flush=True)
    async with core_db.get_sessionmaker()() as session:
        chunks = await session.scalar(select(func.count()).select_from(PostingChunk))
        size = await session.scalar(
            select(func.pg_size_pretty(func.pg_total_relation_size("posting_chunks")))
        )
    print(f"chunked {total} postings; {chunks} chunks in posting_chunks ({size})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
