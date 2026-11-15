# Optional vector backends and retrieval benchmark

PostgreSQL remains the system of record. `VECTOR_BACKEND=pgvector` keeps the exact chunk search as the default. `pgvector_hnsw` and `redis_hnsw` accelerate corpus search for the 384-dimensional `posting_chunks` table. Bounded keyword candidate reranking stays exact, and the separate 2,560-dimensional Qwen path retains its existing PostgreSQL implementation. `packages/matching/retrieve.py` calls the shared chunk-search boundary; keyword gates, fusion, location rules, and citations stay there.

## Setup

Requires PostgreSQL 16 with pgvector **0.8+** for iterative HNSW scans. The checked-in Compose image provides this. The additive migration creates a `halfvec_cosine_ops` HNSW index (`M=16`, `ef_construction=200`); on a large corpus, plan time for index creation before restarting normal work. The migration's downgrade removes only that index.

```bash
make migrate
# PostgreSQL HNSW needs no extra service:
VECTOR_BACKEND=pgvector_hnsw make api

# Optional Redis Stack, loopback only:
.venv/bin/pip install -e '.[redis]'
docker compose --profile redis up -d --wait redis
make sync-vectors
VECTOR_BACKEND=redis_hnsw make api
```

`REDIS_URL` defaults to `redis://127.0.0.1:6379/0`. Keep it local. `VECTOR_EF_SEARCH=100` controls query effort; PostgreSQL's ef_search is capped at 1000. Model identity tags are SHA-256 strings, so embeddings never cross model boundaries. Missing Redis dependencies, an unsynced index, and connection failures are reported rather than silently changing the selected backend. Return to `pgvector` to use PostgreSQL alone.

## Sync and consistency

`scripts/sync_vectors.py` reads a repeatable PostgreSQL snapshot in batches, hashes vectors plus metadata, and pipelines writes only for changed documents. Each Redis HASH contains chunk/posting IDs, model/company/remote/open tags, span offsets, a source content hash, and a FLOAT32 vector. No candidate, résumé, browser state, or credential is copied. An advisory transaction lock serializes syncs on the source database. Deletions and stale source chunks are reconciled with namespace-scoped `SCAN` and bounded deletes; interruption is safe to retry.

Reconciliation reads the corpus on each run: posting metadata has no universal update timestamp, and deletion cannot be captured with an insert watermark. Redis writes are incremental; this is not CDC or an automatic background schedule. Run `make sync-vectors` after chunking, crawling, closing, or deleting postings, or invoke it from your own local scheduler. Re-sync after recreating the Redis container. One application corpus owns the default index namespace; benchmarks and integration tests use separate namespaces.

Redis provides candidate chunk IDs. PostgreSQL rechecks current content hashes, embedding models, company, remote and open predicates, and computes final distances from source vectors. Metadata changes cannot expose an ineligible hit between syncs; newly eligible or newly inserted records can be absent until sync. Cached vector updates can affect recall until synchronization. Approximate search can miss neighbors even in a fresh cache. Redis partial results are not proof of corpus completeness.

PostgreSQL HNSW searches chunk candidates before joining metadata, widening when filters leave too few. Redis applies TAG predicates in its KNN query. Query vectors are quantized to the same halfvec representation before comparison across engines, and Redis candidates are reranked in PostgreSQL. Zero vectors are omitted from cosine indexing. Stable-ID tie breaking resolves exact ties; ANN ties can still select different boundary neighbors.

## Reproduce the benchmark

```bash
make benchmark-vectors
# Larger local experiment:
make benchmark-vectors ARGS='--chunks 20000 --queries 100 --seed 42'
# PostgreSQL only:
make benchmark-vectors ARGS='--backends pgvector pgvector_hnsw'
# Optional real Redis integration tests (disposable marked test DB):
RUN_REDIS_TESTS=1 REQUIRE_DB=1 .venv/bin/pytest -q tests/test_vector_backends.py
```

The harness creates a uniquely named disposable PostgreSQL schema, synthetic vectors, and a Redis index namespace. It removes these in `finally` and never updates application rows or the application's Redis index. The configured database account needs schema-creation rights; use a dedicated development database. An interrupted process can leave a `vector_bench_*` schema or `jobrunner:chunks:v1:bench:*` namespace to remove after confirming it belongs to that run.

Default: 5,000 chunks, 50 independent queries, seed 42, dimension 384, three warmup queries. NumPy creates deterministic normalized vectors; PostgreSQL stores halfvec values and supplies exact cosine ground truth. Each synthetic posting has one chunk. Four scenarios cover open-only, remote/open, company/open, and company/remote/open searches. Backend order rotates deterministically per query. p50/p95 measure warm client round trips, including validation/reranking and Redis calls where applicable, excluding embeddings, index construction, and sync.

The harness disables JIT and discourages sequential scans to exercise the ANN index on small fixtures; the exact query uses `distance + 0` so it cannot use ANN ordering. Reports contain a representative PostgreSQL candidate query plan; verify it names `ix_posting_chunks_embedding_hnsw`. Normal application queries leave planner choices enabled. Small/selective datasets may favor exact scans, and HNSW is not assumed faster.

Results are written to ignored `storage/vector-benchmark.json`: corpus/query SHA-256, seed, dimensions, sample counts, engine/Python/NumPy versions, HNSW settings, query plan, sync stats, and per-scenario latency plus mean **chunk Recall@10** against exact scan. These synthetic figures measure index fidelity, not relevance to human questions, posting-level recall across multiple chunks, production throughput, or end-to-end Q&A quality. Run on representative data before changing a default.

API syntax follows the primary [Redis vector-search documentation](https://redis.io/docs/latest/develop/ai/search-and-query/vectors/) and [pgvector documentation](https://github.com/pgvector/pgvector#hnsw).

## Example local result

[Recorded run and environment](evaluations/vector-backends-synthetic-2026-11-14.json): 5,000 chunks, 50 queries, seed 42. Open-only search measured:

| Backend | p50 | p95 | Recall@10 |
| --- | --- | --- | --- |
| pgvector exact | 4.59 ms | 6.83 ms | 1.000 |
| pgvector HNSW | 7.34 ms | 10.07 ms | 0.788 |
| Redis HNSW | 4.12 ms | 5.79 ms | 1.000 |

This was one local run alongside a Docker build; timings are illustrative and graph construction can vary. The lower PostgreSQL ANN recall and its overhead at this size support retaining exact scan as the default. Redis Search can choose brute-force evaluation for filtered hybrid queries internally; these numbers describe its configured HNSW backend, not a guarantee that every filtered query traverses the graph. All four scenarios and query-plan evidence are in the report.
