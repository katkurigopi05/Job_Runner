"""Reconcile the disposable Redis HNSW index from PostgreSQL; repeat safely."""

import argparse
import asyncio
import json

from packages.core.db import get_engine, get_sessionmaker
from packages.matching.vector_search import INDEX, redis_client
from packages.matching.vector_sync import sync


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--index", default=INDEX, help="Redis index namespace (default: application)"
    )
    args = parser.parse_args()
    client = redis_client()
    try:
        async with get_sessionmaker()() as session, session.begin():
            print(json.dumps(await sync(session, client, index=args.index), sort_keys=True))
    finally:
        await client.aclose()
        await get_engine().dispose()


if __name__ == "__main__":
    asyncio.run(main())
