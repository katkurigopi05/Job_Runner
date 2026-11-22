"""Backfill and compare the Qwen 2,560-dimensional PostgreSQL and Redis paths."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
from time import perf_counter
from uuid import UUID, uuid4

import numpy as np
from sqlalchemy import exists, insert, select, text

from packages.core.db import get_sessionmaker
from packages.core.models import Application, Company, Posting
from packages.core.models_chunks import LARGE_EMBEDDING_DIM, PostingChunk, PostingChunkVector
from packages.core.models_interview import InterviewPlan, InterviewRound, InterviewVector
from packages.interview import retrieval
from packages.interview.schema import Evidence
from packages.matching import query_vectors, qwen_redis
from packages.matching.chunk_vectors import QWEN_4B
from packages.matching.chunks import OVERLAP
from packages.matching.vector_search import Filters
from scripts.seed_interview import seed

SEED = 42
CORPUS_SIZE = 512


async def _fixture(session):
    if await session.scalar(
        select(
            exists().where(PostingChunkVector.embedding_model == QWEN_4B)
            | exists().where(InterviewVector.model == QWEN_4B)
        )
    ):
        raise ValueError("Use a migrated demo database with no existing Qwen vectors.")
    app = await seed(session, commit=False)
    rng = np.random.default_rng(SEED)
    vectors = rng.normal(size=(CORPUS_SIZE, LARGE_EMBEDDING_DIM)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    company_ids, round_ids = [uuid4(), uuid4()], [uuid4(), uuid4()]
    await session.execute(
        insert(Company),
        [
            {"id": identifier, "name": f"Benchmark fixture {number}"}
            for number, identifier in enumerate(company_ids)
        ],
    )
    await session.execute(
        insert(InterviewRound),
        [{"id": identifier, "application_id": app.id} for identifier in round_ids],
    )
    plan_ids = [uuid4(), uuid4()]
    await session.execute(
        insert(InterviewPlan),
        [
            {
                "id": plan,
                "round_id": round_,
                "request_id": uuid4(),
                "inputs": {"fixture": True},
                "progress": {},
                "status": "local",
            }
            for plan, round_ in zip(plan_ids, round_ids, strict=True)
        ],
    )
    posting_ids = [uuid4(), uuid4()]
    await session.execute(
        insert(Posting),
        [
            {
                "id": identifier,
                "url": f"https://benchmark.example.com/{identifier}",
                "company_id": company,
                "content_hash": "fixture",
            }
            for identifier, company in zip(posting_ids, company_ids, strict=True)
        ],
    )
    for start in range(0, len(vectors), 64):
        batch = [
            (n, uuid4(), values.tolist())
            for n, values in enumerate(vectors[start : start + 64], start=start)
        ]
        await session.execute(
            insert(PostingChunk),
            [
                {
                    "id": chunk_id,
                    "posting_id": posting_ids[n % 2],
                    "ordinal": n,
                    "start": n,
                    "length": 10,
                    "overlap": OVERLAP,
                    "content_hash": "fixture",
                }
                for n, chunk_id, _ in batch
            ],
        )
        await session.execute(
            insert(PostingChunkVector),
            [
                {"chunk_id": chunk_id, "embedding_model": QWEN_4B, "embedding": values}
                for _, chunk_id, values in batch
            ],
        )
        await session.execute(
            insert(InterviewVector),
            [
                {
                    "id": uuid4(),
                    "plan_id": plan_ids[n % 2],
                    "source_id": str(n),
                    "kind": "project",
                    "model": QWEN_4B,
                    "embedding": values,
                }
                for n, _, values in batch
            ],
        )
    company = await session.scalar(select(Posting.company_id).where(Posting.id == app.posting_id))
    return (
        [row.tolist() for row in vectors[:24]],
        app.id,
        company,
        company_ids[0],
        round_ids[0],
    )


def _metrics(samples):
    latency = {}
    for backend in ("pgvector", "redis"):
        values = [sample[backend] for sample in samples]
        p50, p95 = np.percentile(values, [50, 95])
        latency[backend] = {"p50_ms": round(float(p50), 3), "p95_ms": round(float(p95), 3)}
    return {
        "queries": len(samples),
        "nonempty_exact": sum(sample["exact_count"] > 0 for sample in samples),
        "recall_at_10": round(
            float(np.mean([sample["recall"] for sample in samples if sample["exact_count"]])), 5
        )
        if any(sample["exact_count"] for sample in samples)
        else None,
        **latency,
    }


async def _measure(call, queries):
    samples = []
    for vector in queries:
        hits = {}
        sample: dict = {}
        for name in ("pgvector", "redis"):
            start = perf_counter()
            hits[name] = set(await call(vector, name == "redis"))
            sample[name] = (perf_counter() - start) * 1000
        exact = hits["pgvector"]
        sample["exact_count"] = len(exact)
        sample["recall"] = len(exact & hits["redis"]) / len(exact) if exact else None
        samples.append(sample)
    return _metrics(samples)


async def _index_stats(client, index):
    response = await client.execute_command("FT.INFO", index)
    info = dict(zip(response[::2], response[1::2], strict=True))
    fields = (
        "num_docs",
        "vector_index_sz_mb",
        "total_index_memory_sz_mb",
        "inverted_sz_mb",
        "hash_indexing_failures",
    )
    return {key: float(info[key.encode()]) for key in fields if key.encode() in info}


async def benchmark(session, client, args, memory_before):
    start = perf_counter()
    if args.synthetic:
        vectors, application, company, posting_company, round_id = await _fixture(session)
        suffix = f":fixture:{uuid4().hex}"
    else:
        if not args.application or not args.round or not args.company or not args.query:
            raise ValueError("Live comparison needs --application, --round, --company and --query.")
        application, posting_company, round_id = args.application, args.company, args.round
        company = await session.scalar(
            select(Posting.company_id)
            .join(Application, Application.posting_id == Posting.id)
            .where(Application.id == application)
        )
        vectors = [list((await query_vectors.embed_question(query)).values) for query in args.query]
        suffix = ""
    indexes = (qwen_redis.INDEX + suffix, retrieval.INDEX + suffix)

    async def _posting(vector, redis, filters):
        return [
            hit.chunk_id
            for hit in await qwen_redis.search(
                session,
                vector,
                redis=redis,
                filters=filters,
                client=client,
                index=indexes[0],
            )
        ]

    async def _evidence(vector, redis, **scope):
        return [
            identifier
            for identifier, _ in await retrieval.search(
                session,
                vector,
                redis=redis,
                application_id=application,
                client=client,
                index=indexes[1],
                **scope,
            )
        ]

    try:
        posting_sync = await qwen_redis.sync(session, client, index=indexes[0], batch_size=128)
        sync_started = perf_counter()
        evidence_sync = await retrieval.sync(session, client, index=indexes[1], batch_size=128)
        evidence_sync["seconds"] = round(perf_counter() - sync_started, 4)
        samples = {
            "posting": await _measure(lambda v, r: _posting(v, r, Filters()), vectors),
            "posting_company": await _measure(
                lambda v, r: _posting(v, r, Filters(companies=(posting_company,))), vectors
            ),
            "application": await _measure(_evidence, vectors),
            "application_round_company": await _measure(
                lambda v, r: _evidence(v, r, round_id=round_id, company_id=company), vectors
            ),
        }
        memory = (await client.info("memory"))["used_memory"]
        return {
            "synthetic": args.synthetic,
            "seed": SEED if args.synthetic else None,
            "platform": platform.platform(),
            "dimensions": LARGE_EMBEDDING_DIM,
            "measurement_seconds": round(perf_counter() - start, 3),
            "sync": {"postings": posting_sync, "interview_evidence": evidence_sync},
            "redis": {
                "memory_bytes": memory,
                "memory_delta_bytes": memory - memory_before,
                "posting_index": await _index_stats(client, indexes[0]),
                "interview_index": await _index_stats(client, indexes[1]),
            },
            "postgres_relation_bytes": dict(
                (
                    await session.execute(
                        text(
                            "SELECT relname, pg_total_relation_size(oid) FROM pg_class "
                            "WHERE relname IN ('posting_chunk_vectors', 'interview_vectors')"
                        )
                    )
                ).all()
            ),
            "query_groups": samples,
        }
    finally:
        if args.synthetic:
            for index in indexes:
                try:
                    await client.execute_command("FT.DROPINDEX", index, "DD")
                except Exception as exc:
                    if "unknown index" not in str(exc).lower():
                        raise
                await client.delete(f"{index}:synced", f"{index}:cursor")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync")
    sync.add_argument("corpus", choices=("postings", "interviews"))
    sync.add_argument("--batch-size", type=int, default=500)
    sync.add_argument("--max-batches", type=int)
    sync.add_argument("--rebuild", action="store_true")
    backfill = commands.add_parser("embed")
    backfill.add_argument("plan", type=UUID, nargs="+")
    compare = commands.add_parser("compare")
    compare.add_argument("--synthetic", action="store_true")
    compare.add_argument("--application", type=UUID)
    compare.add_argument("--company", type=UUID)
    compare.add_argument("--round", type=UUID)
    compare.add_argument("--query", action="append")
    args = parser.parse_args()
    async with get_sessionmaker()() as session:
        if args.command == "embed":
            for plan_id in args.plan:
                plan = await session.get(InterviewPlan, plan_id)
                if plan is None:
                    raise ValueError(f"Unknown plan {plan_id}")
                ids = await retrieval.embed_checkpoint(
                    session, plan, [Evidence.model_validate(s) for s in plan.progress["evidence"]]
                )
                await session.commit()
                await retrieval.publish(session, ids)
                print(json.dumps({"plan": str(plan_id), "embedded": len(ids)}))
            return
        async with retrieval.local_redis() as client:
            if args.command == "compare":
                memory_before = (await client.info("memory"))["used_memory"]
                result = await benchmark(session, client, args, memory_before)
            else:
                runner = qwen_redis if args.corpus == "postings" else retrieval
                result = await runner.sync(
                    session,
                    client,
                    batch_size=args.batch_size,
                    max_batches=args.max_batches,
                    rebuild=args.rebuild,
                )
            print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
