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
