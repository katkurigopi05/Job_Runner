# Job_Runner

A local job-search and application workbench that turns postings into evidence-backed matches, tailored résumés, and reviewable application packets. You own the data and approve what gets submitted.

Built to explore the engineering behind dependable AI workflows: async ingestion, retrieval evaluation, source attribution, browser automation, durable queues, and human review.

## Architecture

```mermaid
flowchart LR
    UI[Next.js 15 dashboard] --> API[FastAPI · loopback only]
    API --> DB[(PostgreSQL · asyncpg · pgvector)]
    DB --> Q[Postgres queue · SKIP LOCKED]
    Q --> W[Playwright worker · ATS adapters]
    W --> R[Pluggable LLM Router]
    R --> G[Fabrication Guard]
    G --> H[Human review · AUTO_SUBMIT=false]
    H --> W
    API --> S[Grounded retrieval and Q&A]
    S --> DB
    I[Linked invitation or phone note] --> UI
    API --> IP[Interview rounds and prep plans]
    IP --> DB
    Q --> IW[Interview worker checkpoints]
    IW --> PUB[Public search, URLs and GitHub excerpts]
    IW --> K[Kaggle Qwen 27B]
    IW --> QE[Local Qwen 4B embeddings]
    IW --> DB
    QE --> DB
    DB --> RC[(Optional Redis HNSW cache)]
```

PostgreSQL is the system of record. Résumés, browser sessions, and screenshots stay in local storage. The dashboard proxies API requests over loopback. This is a single-user development tool; it has no network-facing authentication layer.

## Engineering highlights

- **Adversarial Tailoring Guard:** source-scoped evidence checks reject invented skills, dates, credentials, metrics, and claims borrowed from another résumé entry. Tests include adversarial rewrites; review exposes accepted changes and rejected attempts.
- **ATS Contract:** adapters in `packages/ats/` share inspection, field mapping, fill reports, and explicit failure reasons. Unknown answers park the application with the employer's exact question. CAPTCHA and unsupported flows require manual completion.
- **Grounded Q&A:** keyword constraints, model-specific chunk retrieval, reciprocal rank fusion, and citations tie answers to stored postings. Counts and application states come from SQL. Retrieval benchmarks measure fidelity separately from human relevance.
- **Graph retrieval:** project technologies connect to directly required job skills, with source-backed paths and recurring résumé skill gaps in the assistant. See [GraphRAG scope and sources](docs/GRAPH_RAG.md).
- **Job-first project tailoring:** Resume Studio can check current public projects through GitHub MCP, rank by direct requirement coverage, and preview guarded description rewrites using Kaggle 27B. Saving retains the approved wording for PDF and DOCX without changing imported project records.
- **Live GitHub code evidence:** Resume Studio can match a posting to bounded, commit-pinned source checks and propose project claims for your approval. See [Live GitHub code evidence](#live-github-code-evidence).
- **Durable execution:** PostgreSQL leases and `FOR UPDATE SKIP LOCKED` support retries; a state machine and append-only events record progress. A submission ledger fences duplicate attempts.
- **Replaceable model providers:** local and explicitly selected remote providers share routing and audit boundaries. No production request silently falls back to test stubs.

## Quickstart

Prerequisites: Python 3.12, Node.js 22, npm, Docker with Compose v2, and native PDF libraries. On macOS: `brew install pango cairo gdk-pixbuf libffi`. On Debian/Ubuntu: `sudo apt-get install -y libpango-1.0-0 libpangocairo-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 libffi8` (see also [Dockerfile](Dockerfile)).

```bash
make demo
```

This installs missing dependencies, starts loopback-only PostgreSQL, migrates a separate `jobrunner_demo` database, seeds synthetic fixtures, and starts the API and dashboard. Open **http://127.0.0.1:3001**. Ports 8000 and 3001 must be available. Ctrl-C stops the foreground demo services; `make down` stops the database without deleting its volume.

Explore `/matches` (10 scored jobs), `/review` (a résumé diff and an unanswered question), `/resumes` (internships, certifications, and three synthetic GitHub imports), and `/tracker` (interview, assessment, awaiting, and rejected outcomes). Rerunning preserves reviewer edits. All job links use reserved `demo.invalid`; nothing is sent. Fixture scores are illustrative. The demo starts no worker and needs no LLM or GitHub credentials. Live assistant generation requires a configured local model.

To prepare without starting servers: `DEMO_PREPARE_ONLY=1 make demo`. The isolated settings and artifacts live under ignored `storage/demo/`. Set `DEMO_ROOT` to an absolute temporary directory to keep all demo files outside the checkout. If the default ports are occupied, use `DEMO_API_PORT=18000 DEMO_WEB_PORT=13001 make demo`.

## Interview prep

```bash
DEMO_INTERVIEW=1 make demo
```

1. In **Tracker**, open **Prepare for interview** on the synthetic application at `interview-demo.example.com`. Choose **Confirm a new round**, select the fixture invitation, and confirm its suggested date, time zone, and topics.
2. Open **What should I practice in the next hour?** and **Requirement coverage**. The synthetic guide supports Python practice, the repository excerpt supports a project discussion, and production Kubernetes appears as a gap. Expand **Research and sources** to see why the senior system-design report was discounted.
3. Start a mock interview, answer the question and follow-up, then review the three rubric comments. Record what was actually asked under **After the interview**. Use **Reschedule or correct this round** to preserve the earlier revision. Open **Research decisions and checkpoints** to inspect the worker's selected action and evidence count.

For a live application, migrate and use the same tracker action. Pick the linked invitation or enter a phone note. With older submissions, select the version actually sent or mark it unknown. If the posting record is missing or changed, paste the description from your original copy. To run just interview work, start `.venv/bin/python -m apps.worker.run --only interview_prep`. Existing custom worker allowlists need **Interview prep** enabled in **Worker controls**.

Configure [Kaggle Qwen 27B](docs/KAGGLE_ASSISTANT.md) and opt in on the confirmation form to use live planning and synthesis. The separate recruiter-mail checkbox controls inclusion of invitation or phone-note excerpts. The mock has its own per-turn sharing control. The browser shows the saved local plan and evidence during a provider outage. A queued retry continues from the last saved batch. After retries are exhausted, **Resume research** can enqueue that revision again. Set `INTERVIEW_BRAVE_API_KEY` for search, or supply public URLs for research without a search key.

For a reproducible Chromium check against the running demo, run
`cd apps/web && node e2e/interview-live.mjs`. Use `CHROMIUM_EXECUTABLE` to point at an existing Chromium binary.

### Compare retrieval and inspect recovery

```bash
# Local PostgreSQL migrated to head, with the Docker Compose Redis profile running.
.venv/bin/python -m pip install -e '.[redis]'
docker compose --profile redis up -d redis
# Use a clean demo database for the seeded comparison.
JOBRUNNER_ENV_FILE=storage/demo/demo.env .venv/bin/python -m scripts.interview_vectors compare --synthetic
RUN_REDIS_TESTS=1 .venv/bin/pytest -q tests/test_interview_prep.py tests/test_vector_backends.py
JOBRUNNER_ENV_FILE=storage/demo/demo.env .venv/bin/python -m scripts.eval_interview
```

Redis is optional. Set `INTERVIEW_QWEN_EMBEDDINGS=true` with the [local Qwen query embedder](docs/VECTOR_BACKENDS.md) to index new evidence. For saved plans, run `python -m scripts.interview_vectors embed PLAN_ID`. The two cache namespaces are independent of the 384-dimensional posting index. Run `python -m scripts.interview_vectors sync postings` and `… sync interviews` to reconcile updates and deletions. Use `--batch-size` and `--max-batches` to checkpoint a partial pass, and `--rebuild` to recreate the selected cache. Set `QWEN_VECTOR_BACKEND=redis_hnsw` after syncing, and switch it back to `pgvector` to use the default PostgreSQL paths. The live comparator accepts `compare --application APP_ID --round ROUND_ID --company COMPANY_ID --query "a retrieval question"`, with repeatable `--query` flags.

**Measured in the local Linux workspace** with PostgreSQL 17.12, pgvector 0.8.0, Redis 7.4.7 and RediSearch 2.10.20. The three-vCPU AMD EPYC 9V74 seed-42 run used 512 synthetic 2,560-dimensional vectors per index and 24 queries per group. Times include PostgreSQL validation of Redis hits.

| Query group | Recall@10 | Exact PG p50 / p95 | Redis p50 / p95 |
| --- | ---: | ---: | ---: |
| Posting chunks | 1.000 | 10.95 / 13.25 ms | 5.83 / 9.07 ms |
| Company-filtered chunks | 1.000 | 6.09 / 9.27 ms | 4.86 / 8.54 ms |
| Application evidence | 1.000 | 6.22 / 8.92 ms | 6.50 / 9.30 ms |
| Application, company and round | 1.000 | 4.92 / 5.77 ms | 5.02 / 7.03 ms |

Sync took 0.73 s for postings and 0.57 s for interview evidence. Redis memory increased by 40.2 MB across both indexes and their hashes. Each vector index reported 10.28 MiB. PostgreSQL reported 3.38 MB for the posting vector relation and 3.30 MB for the interview vector relation. The queries reuse a small set of generated vectors. This run measures index fidelity and loopback cost. Representative production-corpus recall, end-to-end relevance, and live Kaggle latency need live data. PostgreSQL remains the default.

`tests/test_interview_prep.py` exercises the fixture's cited Python project, the unsupported production requirement, and interruption recovery. In the recovery case, the second worker attempt reuses the public excerpt checkpoint and completes the plan without re-reading it. Grade evidence from **Grade evidence relevance** on a plan. The evaluation command reports citation resolution, relevance of judged coverage excerpts, and the independent live label count separately from ANN recall. With at least 30 independent labels across five companies, it compares a ridge ranker and the baseline on a company-held-out judged pool in shadow. The workspace had **zero independent live labels**. No ranker was trained or promoted.

## Configuration

For your own profile, copy `.env.example` to `.env`, then run:

```bash
make install
make nltk-data
playwright install chromium
make up
make migrate
make web-install
make doctor
# Separate terminals:
make api
make web
make worker  # starts real queued work; configure your profile first
```

Select `LLM_PROVIDER=ollama` (and install/pull the local model) before real tailoring; `.env.example` sets `LLM_PROVIDER=stub` for tests and offline checks. See [docs/USAGE.md](docs/USAGE.md) for provider setup, dashboard routes, and troubleshooting.

The complete environment template is [.env.example](.env.example); defaults are defined in `packages/core/config.py`.

| Setting | Purpose |
| --- | --- |
| `DATABASE_URL` | PostgreSQL with pgvector; Compose uses a public local-development credential |
| `AUTO_SUBMIT=false` | Keep the approval gate; profiles also have their own opt-in |
| `LLM_PROVIDER`, `LLM_TASK_*` | Select model routing; `stub` is for tests/demo fixtures, `ollama` for local inference |
| `OLLAMA_*`, provider API keys | Configure only providers you explicitly choose; remote calls can transmit résumé text |
| `GITHUB_USERNAME`, `GITHUB_TOKEN` | Project import. The existing read-only token also enables indexed code search and permitted private repository reads. |
| `TAILOR_LIVE_GITHUB_CODE` | Add live GitHub coverage to automatic application review. Defaults to `false`. |
| `VAULT_KEY`, `VAULT_ROOT` | Encrypt ATS credentials; generate a key with `make vault-key` |
| `STORAGE_ROOT` | Local résumés, artifacts, and browser state; excluded from git and Docker |
| `JOBRUNNER_ENV_FILE` | Select a private settings file outside the checkout |
| `VECTOR_BACKEND` | `pgvector` (exact default), `pgvector_hnsw`, or optional `redis_hnsw` |

Optional vector setup and a reproducible latency/Recall@10 benchmark: [docs/VECTOR_BACKENDS.md](docs/VECTOR_BACKENDS.md). For live workflows, see [docs/USAGE.md](docs/USAGE.md). Architecture specifications live in [CLAUDE.md](CLAUDE.md); design experiments and limitations live under `docs/`.

## Live GitHub code evidence

Sync projects through the existing GitHub connection. Open **Resume Studio**
at `/resumes/studio`, paste the job description, select **Use live GitHub code
evidence**, and synthesize a draft. Turn on model rewriting if you also want
guarded revisions to bullets from your résumé and confirmed records.

Studio prioritizes required skills, shows preferred skills and alternatives, and
compares them with the résumé and confirmed internship and certification records.
The GitHub check ranks relevant imported repositories, reads their current
default-branch commit and tree, and verifies only selected file blobs. GitHub's
indexed code-search results suggest paths. If the index is unavailable the
pinned tree can still provide verifiable matches. Each finding is labeled
**Declared**, **Implemented**, **Documented**, or **Insufficient**.

Choose wording and confirm your contribution for each Projects claim you want to
keep. Leave a claim unchecked to skip it. Saving rechecks approved claims at the
current commit and reads back the rendered PDF before creating a new version.
Approved claims become Projects bullets in that version.
For example, a Python handler with an AWS Lambda response in
`src/lambda.py` supports the suggestion “Implemented a Python handler for AWS
Lambda.” The file and line range appear in Studio. Only the approved wording
appears in the résumé.

The check reads up to four repositories and ten files per repository, with a
64 KiB limit per source file. Forks and excluded repositories are skipped.
Private repositories need a token with read access and remain subject to the
existing Projects inclusion rules. Repository code stays out of model prompts,
and private permalinks stay out of résumé output. A small in-memory cache holds
derived signals for public files and uses the current commit in its key.

Implementation detection covers selected Python, FastAPI, AWS, Docker,
Kubernetes, and GitHub Actions patterns. Static checks cannot establish runtime
behavior, production deployment, or authorship. Source the rules cannot validate
may still appear as a declaration or documentation. The optional worker setting
adds a coverage report to application review. New code-backed claims are approved
in Studio through the existing résumé version workflow.

## Development and license

See [CONTRIBUTING.md](CONTRIBUTING.md) for checks and ATS adapter development, and [SECURITY.md](SECURITY.md) for the trust model and vulnerability reporting. [MIT licensed](LICENSE).
