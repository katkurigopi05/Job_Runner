# Deferred Work and Operational Boundaries

This document records deferred work, known operational limitations, and architecture boundaries for `Job_Runner` following the targeted tailoring and retrieval baseline integration (2026-10-13).

## 1. Deferred Work

### 1.1 Operational-Rate Reporting
- **Current status**: Signals are recorded in crawl records, matches, application events, inbound messages, and the local LLM audit journal. They are not yet combined into an operational-rates dashboard.
- **Rates to report**:
  - Ingestion failure rate (crawled postings rejected by parser or schema).
  - Stale posting prune rate (postings closed or expired under the configured retention policy).
  - Cache hit rate (tailoring cache hits vs. fresh LLM runs).
  - Review approval rate (owner approvals vs. rejections at review gate).
- **Reason deferred**: The application is single-user and local; querying these metrics directly from PostgreSQL when needed is sufficient without background aggregation services.

### 1.2 Generated-Answer Grounding Evaluation
- **Current status**: Deterministic retrieval correctness tests, known-target retrieval rankings, and citation checks per reply are implemented. Groundedness of generated prose across wide query sets is not yet automatically scored.
- **Reason deferred**: Meaningful answer-grounding evaluation cannot be synthesized from mock fixtures. It requires genuine human relevance labels from the owner; the local prototype supplies none. Running LLM-as-a-judge over ungrounded labels would produce unverified metrics.

### 1.3 IMAP bootstrap history policy
- **Implemented**: `packages/inbox/imap.py` reads with `BODY.PEEK` and uses a durable UID checkpoint. After initialization, polls fetch UIDs after that checkpoint regardless of the `SEEN` flag; opening a new reply on another device does not skip it.
- **Bootstrap boundary**: the first poll, or a mailbox UIDVALIDITY change, searches `UNSEEN` and establishes a high-water mark. Messages already read before that initial snapshot are not imported automatically.
- **Remaining decision**: an explicit bounded historical import could include previously read messages. It must preserve mailbox flags and message de-duplication, and needs an owner-selected time/UID boundary. No mailbox policy changes are made by this document.

---

## 2. LlamaIndex prototype: not a validated comparison

The local sentence-window adapter and evaluation script are review prototypes, not a released replacement for production retrieval. Their current comparison indexes only target postings in LlamaIndex while production searches its full corpus; filter behavior also differs. Those results cannot rank the engines fairly.

Before publication or promotion, use identical frozen corpora, embeddings and metadata filters, record the effective embedding backend, test index rebuilds and optional-dependency failures, and derive every finding from measured results. Human relevance metrics require owner grades; target retrieval alone does not establish answer grounding. No chunk-expansion ratio, latency advantage, or false-positive conclusion is claimed here.

Production retrieval defaults remain PostgreSQL/pgvector. Optional Redis HNSW is described separately below.

---

## 3. Deliberately Excluded Complexity (Anti-Patterns)

The following architectural additions are rejected as unjustified for a local, single-user system:
- **Distributed Vector Stores (Pinecone, Qdrant, Milvus)**: The application already has a PostgreSQL source of record and optional local HNSW acceleration; a distributed vector service has not been justified by a reproducible workload.
- **Multi-Tenant Access Control & Enterprise Auth**: The application runs loopback only for a single candidate.
- **Separate Microservices / Parser Daemons**: The existing asynchronous worker and PostgreSQL queue avoid introducing a separate broker or parser service.
- **Autonomous Multi-Agent Orchestration**: Direct deterministic pipeline execution with explicit review gates preserves user agency and auditability.

## 4. Optional local HNSW experiment

Redis Stack is now an explicit optional local accelerator (`redis` Compose profile), alongside exact pgvector and pgvector HNSW. PostgreSQL remains authoritative; this does not introduce a distributed system of record. See [VECTOR_BACKENDS.md](VECTOR_BACKENDS.md) for incremental cache reconciliation, consistency limits, and the reproducible p50/p95 + Recall@10 harness. Defaults remain exact PostgreSQL retrieval. Automatic CDC/background sync, Qwen-vector export to Redis, and relevance claims from synthetic benchmark results remain outside this implementation.
