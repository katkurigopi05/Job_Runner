# Job Runner Usage Guide

Job Runner is a local job-search and application assistant. You can use it through the web dashboard, talk to it from an MCP-enabled coding assistant, or call its local API directly.

The safe default is simple: Job Runner may find, prepare, and fill an application, but it stops for your approval before submitting. If it cannot answer a question safely, it also stops and shows you the employer's exact wording.

## Command types at a glance

This project has three kinds of “commands.” They are related, but they are not interchangeable.

| Type | Example | Where to use it |
|---|---|---|
| Shell command | `make api` | A terminal opened in the project directory |
| Dashboard path | `/review` | After `http://localhost:3000`, for example `http://localhost:3000/review` |
| MCP tool | `review_queue` | Called by an MCP-enabled assistant, usually through a plain-language request |

Job Runner currently has no custom literal slash commands such as `/apply` or `/status`. A slash in this guide normally means a dashboard path. When using Claude Code or another MCP client, ask in plain English; the client selects the appropriate Job Runner tool.

## 1. Requirements

Install these before the first run:

- Python 3.12
- Docker Desktop or Docker Engine with Compose
- Node.js and npm
- Chromium for Playwright
- Pango and related native libraries for résumé PDF rendering
- Optional: Ollama for the private, local dashboard assistant

On macOS, install the PDF libraries with:

```bash
brew install pango cairo gdk-pixbuf libffi
```

## 2. First-time setup

Run all commands from the repository root:

```bash
cp .env.example .env
make install
.venv/bin/playwright install chromium
make web-install
make up
make migrate
make doctor
```

What each command does:

- `cp .env.example .env` creates your private local configuration. Never commit `.env`.
- `make install` creates `.venv` with Python 3.12 and installs the backend and development packages.
- `.venv/bin/playwright install chromium` installs the browser version required by the worker.
- `make web-install` installs the dashboard's Node packages.
- `make up` starts Postgres and pgvector in Docker.
- `make migrate` creates or updates the database schema.
- `make doctor` checks the database, migrations, browser, PDF libraries, vault key, and optional local services. It prints the command needed to fix each failed check.

### Database port on this checkout

The committed `docker-compose.yml` publishes Postgres on port `5432`. This checkout also contains `docker-compose.override.yml`, which changes the host port to `5433` because another local database uses `5432`.

Make the URLs in `.env` match the active Compose port:

```dotenv
DATABASE_URL=postgresql://jobrunner:jobrunner@localhost:5433/jobrunner
TEST_DATABASE_URL=postgresql://jobrunner:jobrunner@localhost:5433/jobrunner_test
```

If you remove or do not use the override, use port `5432` instead.

### Create the vault key

The vault encrypts ATS credentials. Generate a key locally:

```bash
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Copy the printed value into `VAULT_KEY=` in `.env`. Do not paste the key into issues, chat messages, screenshots, or commits.

## 3. Start Job Runner

For normal use, keep these processes running in separate terminals.

Terminal 1 — database:

```bash
make up
```

Terminal 2 — API:

```bash
make api
```

The API is available at `http://127.0.0.1:8000`. Interactive API documentation is at `http://127.0.0.1:8000/docs`.

Terminal 3 — worker:

```bash
make worker
```

The worker claims queued tasks, drives the browser, and moves applications into review or completion states. If it is not running, applications remain `queued`.

Terminal 4 — web dashboard:

```bash
make web
```

Open `http://localhost:3000`.

Optional terminal 5 — local assistant:

```bash
ollama serve
```

In another terminal, pull the default model once:

```bash
ollama pull llama3.1
```

The dashboard assistant always uses Ollama locally. Changing `LLM_PROVIDER` does not send dashboard chat to Gemini or Anthropic.

## 4. Create your candidate and profile

The current dashboard edits existing profiles but does not create the first candidate or profile. Create them through the API documentation at `/docs`, through an API client, or with the following `curl` examples.

### Create a candidate

```bash
curl -sS -X POST http://127.0.0.1:8000/candidates \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "Your Name",
    "email": "you@example.com",
    "email_mode": "self"
  }'
```

Copy the returned `id`; this is your `candidate_id`.

### Create a profile

Replace `CANDIDATE_ID` with the ID returned above. Write work authorization exactly as you want it copied onto real forms.

```bash
curl -sS -X POST http://127.0.0.1:8000/profiles \
  -H 'Content-Type: application/json' \
  -d '{
    "candidate_id": "CANDIDATE_ID",
    "label": "Primary",
    "phone": "+1 555 555 0100",
    "location": "San Francisco, CA",
    "work_auth": "YOUR EXACT ANSWER",
    "needs_sponsorship": false,
    "links": {
      "linkedin": "https://www.linkedin.com/in/your-name",
      "github": "https://github.com/your-name"
    },
    "min_match_score": 0.75,
    "auto_submit": false
  }'
```

Copy the returned profile `id`. After this, use the dashboard's `/profile` page for ordinary profile edits.

### Upload a résumé

Open `http://localhost:3000/resumes` and upload a text-based PDF, DOCX, TXT, or Markdown file. Scanned image-only PDFs will not parse reliably.

The dashboard makes the upload the first listed profile's base résumé automatically. Then inspect the parsed preview: if Job Runner does not show a section, an ATS may not see it either. If you keep multiple profiles, use `POST /resumes/{resume_id}/set-base?profile_id={profile_id}` to assign the intended base explicitly.

## 5. Dashboard paths (`/` routes)

The paths below come after `http://localhost:3000`.

| Path | Purpose |
|---|---|
| `/` | Desk: counts, recent activity, top matches, and anything waiting on you |
| `/review` | Inspect applications parked for approval |
| `/finish` | Work through applications and questions that need your action |
| `/matches` | Browse scored job matches and inspect score breakdowns |
| `/swipe` | Mark postings as interested or skipped to calibrate your threshold |
| `/applications` | View the entire application pipeline by status |
| `/applications/{id}` | Inspect one application and its current state |
| `/applications/{id}/apply` | Prepare or complete the manual application handoff |
| `/resumes` | Upload résumés and inspect what the parser extracted |
| `/tracker` | View outcomes, recruiter replies, and follow-up activity |
| `/chat` | Ask the local Ollama assistant about your stored application data |
| `/profile` | Edit the answer set copied onto application forms |

Useful dashboard assistant prompts include:

```text
Which applications are waiting on me right now?
Summarize where my applications stand.
Have I had any replies, and what did they say?
What happened with my application to Example Company?
```

The assistant refuses to invent or choose work-authorization, sponsorship, employment-history, and salary answers. Those values must come from your profile or from you.

## 6. Use Job Runner from Claude Code or another MCP client

The repository's `.mcp.json` registers the Job Runner MCP server. Start the API first:

```bash
make api
```

Open the repository in the MCP-enabled client and ask in plain language. Examples:

```text
Which ATS platforms are supported?
Is https://example.com/jobs/123 a supported application URL?
List my candidates and profiles.
Search indexed postings for backend Python roles in California.
Apply to this URL using my Primary profile: https://example.com/jobs/123
What is in my review queue?
Show the unanswered questions for application APPLICATION_ID.
Approve APPLICATION_ID with these answers: ...
Reject APPLICATION_ID and note that the role is no longer relevant.
Show the complete history for APPLICATION_ID.
Inspect my latest résumé as the parser sees it.
Sync my GitHub projects and preview which four fit this job description.
```

### Use GitHub skills that are missing from your base résumé

First set `GITHUB_USERNAME` in `.env`. A read-only `GITHUB_TOKEN` is optional for public repositories and required when you choose to include private repositories. Then ask your MCP-enabled assistant:

```text
Sync my GitHub projects.
For this posting, show which required skills are supported by my GitHub projects.
Preview the projects that would be added to the tailored résumé and show the evidence.
```

Job Runner compares the posting with source-reported repository metadata: the repository name, description, primary language, and topics. For example, a repository with the topic `time-series`, language `Python`, and a forecasting description can support those terms in the Projects section even when the base résumé does not mention them.

Keep repository descriptions and topics accurate if you rely on this feature. Job Runner does not infer proficiency merely from a repository existing, and it does not move project evidence into an employer's experience bullets. The generated PDF prints the verified language and topics beside the selected project so both a reviewer and an ATS can see why it is relevant.

### MCP tool reference

You normally do not call these by hand. They are listed so you know what the assistant can actually do.

| Tool | What it does |
|---|---|
| `detect_ats` | Detect an ATS from a posting URL |
| `supported_ats` | List ATS adapters Job Runner can drive |
| `search_postings` | Search locally indexed postings |
| `apply_to_url` | Queue an application; it does not immediately submit |
| `application_status` | Get one application's current status and review data |
| `application_history` | Read its append-only event history |
| `list_applications` | List applications, optionally filtered by status |
| `review_queue` | List applications parked at `needs_review` |
| `approve_application` | Record approval and optional answers, then resume work |
| `reject_application` | Reject a parked application permanently |
| `submit_otp` | Supply a requested one-time verification code |
| `list_candidates` | List candidate records and their IDs |
| `list_profiles` | List reusable application profiles and their IDs |
| `list_resumes` | List a candidate's uploaded résumés |
| `inspect_resume` | Show what the résumé parser extracted |
| `preview_resume` | Preview résumé assembly for job text |
| `sync_github_projects` | Import or refresh GitHub repositories |
| `list_projects` | List imported projects |
| `preview_projects` | Rank projects against job text and return matched GitHub evidence terms |
| `curate_project` | Pin a project or exclude it from future selection |

There is deliberately no `submit_now` tool. Approval releases an application to the worker, which enforces the project's submission rules. There is also no tool named `tailor_resume`; `preview_resume` previews assembly and selected projects.

## 7. Typical application workflow

1. Start Postgres, the API, worker, and dashboard.
2. Run `make doctor` and fix blocking failures.
3. Check `/profile` for your exact legal and contact answers.
4. Check `/resumes` to confirm the parser sees every important section.
5. Find a posting in `/matches`, through MCP search, or from a URL you selected yourself.
6. Queue it through an MCP request or `POST /applications`.
7. Let the worker fill the ATS form.
8. Open `/review` or ask, “What is in my review queue?”
9. Inspect the job, filled answers, unanswered questions, résumé, and screenshot.
10. Approve with exact missing answers, reject it, or finish manually if automation is blocked.
11. Check `/applications` and `/tracker` for status and replies.

Application states normally move like this:

```text
queued -> running -> needs_review -> running -> submitted
                    |                    |
                    +-> failed          +-> failed
                        (rejected)           (including manual completion)
```

`needs_otp` means the employer requested a one-time code. `manual_completion_required` means the site needs human action, often because of a captcha or unsupported browser behavior. Job Runner does not bypass those controls.

## 8. Discovery, matching, and maintenance commands

```bash
make validate-seeds
```

Checks `seeds/companies.yaml` before a crawl.

```bash
make discover
```

Runs a broad aggregator discovery pass and promotes resolved company boards into the seed registry. It can be slow and makes network requests.

```bash
make rescore
make rescore p=backend
make rescore dry=1
```

Recomputes match scores for all profiles, one named profile, or in read-only preview mode.

```bash
make workers n=4
```

Runs several queue claimants in one process. Start with `make worker`; use multiple workers only when you understand the browser and queue workload.

```bash
make import-portals f=/path/to/portals.yml ARGS=--dry-run
```

Previews importing a maintained company portal list. Remove `ARGS=--dry-run` after inspecting the result.

## 9. Development and verification commands

| Command | Purpose |
|---|---|
| `make test` | Run the Python test suite; DB tests may skip if Postgres is unavailable |
| `make lint` | Check Ruff lint and formatting |
| `make fmt` | Format code and apply safe Ruff fixes |
| `make typecheck` | Run mypy on the typed Python packages |
| `make check-migrations` | Detect model/schema migration drift |
| `make check` | Run lint, type checking, and tests |
| `make gate-0` | Run the required base gate with database tests enabled |
| `make gate-1` … `make gate-6` | Run phase-specific verification gates |
| `make gate-1-live URL=...` | Validate against a real Greenhouse posting |
| `make gate-2-live URL=...` | Measure filling against a real Greenhouse posting |
| `make revision m="description"` | Generate an Alembic migration after model changes |

Frontend checks run from `apps/web`:

```bash
cd apps/web
npm run lint
npm run typecheck
npm run build
```

## 10. Stop and restart

Stop the foreground API, worker, web server, or Ollama process with `Ctrl+C` in its terminal.

Stop the database container without deleting its volume:

```bash
make down
```

Your Postgres data remains in the Docker volume. Local résumés, PDFs, screenshots, and browser profiles remain under `storage/`.

For the next session:

```bash
make up
make doctor
```

Then restart `make api`, `make worker`, and `make web` in separate terminals.

## 11. Troubleshooting

### `make doctor` reports Postgres unavailable

Run `make up`, then check that `.env` uses the same host port as Compose. This checkout's override expects `5433`.

### The API works, but an application stays `queued`

Start `make worker`. The API creates queue tasks; the worker performs them.

### The dashboard says it cannot reach the API

Start `make api` and keep it on `127.0.0.1:8000`. The web app proxies its `/api/*` requests to that local service.

### The dashboard assistant is unavailable

Run:

```bash
ollama serve
ollama pull llama3.1
```

Ollama is optional for core crawling and application work.

### Playwright cannot find Chromium

Run:

```bash
.venv/bin/playwright install chromium
```

### PDF or WeasyPrint import errors on macOS

Run:

```bash
brew install pango cairo gdk-pixbuf libffi
```

Then restart the API and worker.

### An application asks for missing profile fields

Complete `/profile`, upload a résumé at `/resumes`, and make it the profile's base résumé. Job Runner refuses to queue an application when required candidate data is already known to be incomplete.

### A site presents a captcha or blocks automation

Finish that application manually. Captcha solving, browser-fingerprint spoofing, proxy rotation, and bot-detection evasion are intentionally outside this project's scope.

## 12. Safety rules worth remembering

- Keep `.env`, `.secrets/`, and `storage/` out of Git.
- Leave `AUTO_SUBMIT=false` until you explicitly decide otherwise.
- Review work authorization, sponsorship, salary, and employment-history answers yourself.
- Treat everything in `storage/` as personally identifiable information.
- Do not expose port `8000` publicly. The API is designed for localhost and has no user authentication.
- Use the crawler only for postings you genuinely intend to consider, and preserve its robots.txt and rate-limit behavior.
- Never approve an application until you have inspected the actual answers, résumé, and unresolved questions.
