"""Reproducible chunk Recall@10 and warm p50/p95 latency, no model download.

Creates a disposable PostgreSQL schema and Redis namespace; never modifies
application rows or the application index. Seeded normalized random vectors
are quantized through the same PostgreSQL halfvec representation used in
production. Queries are independent of corpus vectors. This measures index
fidelity, not semantic relevance. Default runs compare all three backends.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import random
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.config import get_settings
from packages.core.db import get_engine
from packages.core.models import EMBEDDING_DIM, Company, Posting
from packages.core.models_chunks import PostingChunk
from packages.matching.vector_search import INDEX, Filters, redis_client, search
from packages.matching.vector_sync import sync


def recall_at_10(expected: list[uuid.UUID], actual: list[uuid.UUID]) -> float:
    return len(set(expected[:10]) & set(actual[:10])) / min(10, len(expected)) if expected else 1.0


def percentile(values: list[float], percent: float) -> float:
    return float(np.percentile(values, percent))


async def benchmark(args: argparse.Namespace) -> dict[str, Any]:
    engine = get_engine()
    token = uuid.uuid4().hex
    schema = f"vector_bench_{token}"
    index = f"{INDEX}:bench:{token}"
    scoped = engine.execution_options(schema_translate_map={None: schema})
    client = redis_client() if "redis_hnsw" in args.backends else None
    rng = np.random.default_rng(args.seed)
    vectors = rng.normal(size=(args.chunks, EMBEDDING_DIM)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    queries = rng.normal(size=(args.queries, EMBEDDING_DIM)).astype(np.float32)
    queries /= np.linalg.norm(queries, axis=1, keepdims=True)
    corpus_hash = hashlib.sha256(vectors.tobytes()).hexdigest()
    ids = [uuid.UUID(int=i + 1) for i in range(args.chunks)]
    companies = [uuid.UUID(int=i + 1) for i in range(4)]
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            conn = await connection.execution_options(schema_translate_map={None: schema})
            await conn.run_sync(lambda c: Company.metadata.create_all(c))
        async with AsyncSession(scoped) as session, session.begin():
            session.add_all(
                [Company(id=c, name=f"Synthetic company {n}") for n, c in enumerate(companies)]
            )
            await session.flush()
            session.add_all(
                [
                    Posting(
                        id=pid,
                        company_id=companies[i % 4],
                        url=f"https://bench.invalid/{i}",
                        content_hash=f"{i:064x}",
                        is_remote=i % 2 == 0,
                        closed_at=datetime.now(UTC) if i % 7 == 0 else None,
                    )
                    for i, pid in enumerate(ids)
                ]
            )
            await session.flush()
            session.add_all(
                [
                    PostingChunk(
                        id=pid,
                        posting_id=pid,
                        ordinal=0,
                        start=0,
                        length=500,
                        content_hash=f"{i:064x}",
                        embedding_model="synthetic-v1",
                        embedding=vectors[i].tolist(),
                    )
                    for i, pid in enumerate(ids)
                ]
            )
        async with scoped.begin() as conn:
            await conn.execute(text(f'ANALYZE "{schema}".posting_chunks'))
            await conn.execute(text(f'ANALYZE "{schema}".postings'))
        sync_stats = None
        if client is not None:
            async with AsyncSession(scoped) as session, session.begin():
                sync_stats = await sync(session, client, index=index)
            # Existing hashes are indexed synchronously; wait for background
            # initial indexing if the Search module reports it still running.
            for _ in range(100):
                info = await client.execute_command("FT.INFO", index)
                fields = dict(zip(info[::2], info[1::2], strict=True))
                if int(fields.get(b"indexing", 0)) == 0:
                    break
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError("Redis index did not finish indexing")
        scenarios = {
            "open": Filters(),
            "remote_open": Filters(remote=True),
            "company_open": Filters(companies=(companies[0],)),
            "company_remote_open": Filters(companies=(companies[0],), remote=True),
        }
        rows = []
        plans = {}
        # Rotate backend order per query to reduce fixed cache-order bias.
        order = random.Random(args.seed)
        async with AsyncSession(scoped) as session:
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            await session.execute(text("SET LOCAL jit = off"))
            # The fixture is small by default. Force the ANN planner choice and
            # record it, otherwise a sequential scan could masquerade as HNSW.
            for scenario, filters in scenarios.items():
                samples: dict[str, dict[str, list[float]]] = {
                    b: {"ms": [], "recall": []} for b in args.backends
                }
                for q in queries[: args.warmup]:
                    for backend in args.backends:
                        await search(
                            session,
                            q.tolist(),
                            "synthetic-v1",
                            backend=backend,
                            filters=filters,
                            client=client,
                            index=index,
                        )
                for q in queries:
                    truth = await search(
                        session, q.tolist(), "synthetic-v1", backend="pgvector", filters=filters
                    )
                    backends = list(args.backends)
                    order.shuffle(backends)
                    for backend in backends:
                        start = time.perf_counter()
                        hits = await search(
                            session,
                            q.tolist(),
                            "synthetic-v1",
                            backend=backend,
                            filters=filters,
                            client=client,
                            index=index,
                        )
                        samples[backend]["ms"].append((time.perf_counter() - start) * 1000)
                        samples[backend]["recall"].append(
                            recall_at_10([h.chunk_id for h in truth], [h.chunk_id for h in hits])
                        )
                for backend, sample in samples.items():
                    rows.append(
                        {
                            "scenario": scenario,
                            "backend": backend,
                            "p50_ms": percentile(sample["ms"], 50),
                            "p95_ms": percentile(sample["ms"], 95),
                            "recall_at_10": float(np.mean(sample["recall"])),
                            "queries": len(queries),
                        }
                    )
            # Save an actual ANN plan for a representative unfiltered vector
            # order; joins/filters can make PostgreSQL choose another plan.
            distance = PostingChunk.embedding.cosine_distance(queries[0].tolist())
            query = (
                select(PostingChunk.id)
                .where(PostingChunk.embedding_model == "synthetic-v1")
                .order_by(distance)
                .limit(100)
            )
            compiled = query.compile(compile_kwargs={"literal_binds": True})
            sql = (
                str(compiled)
                .replace("posting_chunks", f'"{schema}".posting_chunks')
                .replace("JOIN postings", f'JOIN "{schema}".postings')
            )
            plans["pgvector_hnsw"] = (await session.execute(text("EXPLAIN " + sql))).scalars().all()
            if "pgvector_hnsw" in args.backends and not any(
                "ix_posting_chunks_embedding_hnsw" in line for line in plans["pgvector_hnsw"]
            ):
                raise RuntimeError("PostgreSQL did not use the HNSW index; inspect the query plan")
            version = await session.scalar(text("SELECT version()"))
            pgvector_version = await session.scalar(
                text("SELECT extversion FROM pg_extension WHERE extname='vector'")
            )
        return {
            "seed": args.seed,
            "chunks": args.chunks,
            "dimension": EMBEDDING_DIM,
            "query_sha256": hashlib.sha256(queries.tobytes()).hexdigest(),
            "corpus_sha256": corpus_hash,
            "warmup": args.warmup,
            "python": platform.python_version(),
            "platform": {
                "os": platform.system(),
                "release": platform.release(),
                "architecture": platform.machine(),
            },
            "numpy": np.__version__,
            "postgres": version,
            "pgvector": pgvector_version,
            "redis": (await client.info("server"))["redis_version"] if client else None,
            "hnsw": {
                "m": 16,
                "ef_construction": 200,
                "pg_ef_search": min(1000, max(10, get_settings().vector_ef_search)),
                "redis_ef_runtime": max(10, get_settings().vector_ef_search),
                "pg_shortlist_min": 100,
                "pg_shortlist_factor": 4,
            },
            "metric": "chunk Recall@10 against exact halfvec cosine scan",
            "timing": "warm round trip including PostgreSQL revalidation; excludes embedding",
            "sync": sync_stats,
            "plans": plans,
            "results": rows,
        }
    finally:
        try:
            if client is not None:
                try:
                    try:
                        await client.execute_command("FT.DROPINDEX", index, "DD")
                    except Exception as exc:
                        if "Unknown Index name" not in str(exc):
                            raise
                    await client.delete(f"{index}:synced")
                finally:
                    await client.aclose()
        finally:
            try:
                async with engine.begin() as conn:
                    await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            finally:
                await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunks", type=int, default=5000)
    parser.add_argument("--queries", type=int, default=50)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--backends",
        nargs="+",
        choices=["pgvector", "pgvector_hnsw", "redis_hnsw"],
        default=["pgvector", "pgvector_hnsw", "redis_hnsw"],
    )
    parser.add_argument("--output", type=Path, default=Path("storage/vector-benchmark.json"))
    args = parser.parse_args()
    if args.chunks < 20 or args.queries < 1 or not 0 <= args.warmup <= args.queries:
        parser.error("need chunks >= 20, queries >= 1, and 0 <= warmup <= queries")
    report = asyncio.run(benchmark(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    for row in report["results"]:
        print(
            f"{row['scenario']:22} {row['backend']:15} p50={row['p50_ms']:.2f}ms "
            f"p95={row['p95_ms']:.2f}ms recall@10={row['recall_at_10']:.3f}"
        )
    print(f"Report: {args.output}")


if __name__ == "__main__":
    main()
