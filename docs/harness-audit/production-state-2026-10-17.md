# Production state, checked against the pre-production report — 2026-10-17

Checked 2026-10-17 about 23:00 PDT (2026-10-18 06:00 UTC). The initial check
was read-only. Between 23:03 and 23:08 PDT, Codex performed the cutover
recorded below after correcting the worker's code-path finding.

## The report against the running system

| The report says | What is running |
|---|---|
| Phases 1–4 and the harness integration are complete | True of `origin/main` (`8a3ef3c`, CI and CodeQL green). The root checkout, old API and MCP process are on `tmp/all-phases` (`f453a8d`), which does not have #169–#177. The worker is the exception: it was launched with `PYTHONPATH=/private/tmp/jobrunner-inbox-loop`, and that worktree has no content difference from `origin/main`, so it is running the merged queue fencing, audit, checkpoint and inbox-loop code. |
| Local Qwen3 resolves for every task "after restart" | `.env` has `LLM_PROVIDER=ollama` and `LLM_TASK_TAILOR`, `LLM_TASK_COVER_LETTER` and `LLM_TASK_OPEN_ENDED` set to `ollama`. The worker started tonight reads that. The API was not restarted: its server process was last reloaded on 2026-10-15 14:30 PDT, and `/setup/status` reports `LLM_PROVIDER=gemini`. |
| The vault key is repaired | The single `VAULT_KEY` line in `.env` is 44 characters and parses as a Fernet key. The API still holds the old 91-character value, so `/setup/status` shows the vault as needing attention. |
| Worker stopped, one crawl task waiting | Codex started `python -P -m apps.worker.run` at 22:57:25 PDT (pid 46542), using the clean `/private/tmp/jobrunner-inbox-loop` worktree through `PYTHONPATH`. Its tree matches `origin/main`. The crawl is running: 143 board fetches done, 1 running, 545 pending, 2,782 discovery tasks pending. The newest posting was first seen at 06:00:31 UTC. |
| 0 of 100 owner grades, IMAP unconfigured | Confirmed: `posting_labels` holds 0 rows, and no mailbox is configured. |

## Not in the report

- **Schema.** The live database is at `b8f4c2d6e901`. `main` adds
  `c4e8a1b7d290`, which only creates `mailbox_checkpoints`, and `main`'s
  inbox code needs that table.
- **Database role.** The only role that can log in is `jobrunner`, and it is a
  superuser. #170's code is on `main`, but no runtime role has been
  provisioned.
- **Submission ledger.** The 5 legacy `uncertain` rows in `applied_postings`
  are still unreconciled. Applications: 1 `needs_review`, 4 `failed`.
- **Registry.** 6 retired boards are not marked retired (`make registry-sync dry=1`),
  and 10 boards have failed 3 or more times in a row (`make validate-seeds`).
- **Private files.** `/setup` still flags storage, the vault, backups and
  `.env` as living under the iCloud-managed Desktop.
- **Backup.** The newest backup is `jobrunner-20261015T075842Z`, which predates
  #169–#177 and the migration.
- **MCP processes.** This Claude Code session's MCP server (pid 46166) was
  started from `tmp/all-phases`. It has the owner-confirmation fix, because
  `f453a8d` carries it, but not #172 or #173.
- **Stale doc edit.** The uncommitted edit to `docs/harness-audit/phase-4.md`
  says #165 is "still open". It was merged as `50a7eed`.
- **Memory.** Tailoring now runs on the local 8B model. Earlier measurements
  on this 16 GB Mac had it holding about 4.8 GB, with free memory falling to
  17% beside the API, the dashboard and Chromium. Free memory is 41% now,
  while nothing is loaded.

## Production cutover completed

1. Worker pid 46542 stopped cleanly after committing its current company task.
2. Backup `jobrunner-20261018T060411Z` was created and restore-verified: all
   29 tables, 236,354 rows and 224 artifacts matched.
3. A clean detached runtime worktree was created at
   `/private/tmp/jobrunner-main-runtime`, pinned to `origin/main` at `8a3ef3c`.
   The dirty `tmp/all-phases` root checkout was left untouched.
4. Migration `c4e8a1b7d290` created `mailbox_checkpoints` successfully.
5. The old API stopped cleanly. The replacement API runs from the clean main
   worktree and retains the existing storage, vault and backup locations via
   absolute environment paths.
6. The worker restarted from the same clean main worktree and resumed the
   durable crawl queue.
7. `/setup/status` reports API, PostgreSQL, migrations, worker, vault, storage,
   backups, Ollama and required local tools as `ok`. The overall state is
   `attention` only for optional or operational follow-up items.

Long-lived MCP processes still need to be restarted by ending and reopening
their owning IDE/agent sessions; the active Codex session cannot restart
itself while preserving this run.

**Verified by Claude Code at 23:20 PDT.** The API (pid 48432, 127.0.0.1:8000)
and the worker (pid 48474) run from `/private/tmp/jobrunner-main-runtime`, a
clean checkout of `8a3ef3c`. `/setup/status` matches item 7 above, and
`mailbox_checkpoints` exists with 0 rows. Two processes still load code from
the root checkout, which is still on `tmp/all-phases`:

- **The dashboard** (`next dev`, pid 30522, running since Oct 12). It runs
  without #176 and without `main`'s `middleware.ts` and `local-host.ts`. The
  cutover steps above leave it out.
- **MCP servers** started from `.mcp.json`. They run `.venv/bin/python -m
  apps.mcp.server` from the root checkout, so restarting one loads the old
  code again until the root checkout is on `main`.

Switching the root checkout fixes both: `next dev` hot-reloads its five
changed files, and a restarted MCP server loads `main`. It is clean to
switch: the two modified docs are identical on `main`, no untracked file
exists on `main`, and local `main` is an ancestor of `origin/main`:
`git branch -f main origin/main && git checkout main`. Claude Code's attempt
to run this was denied as a production deploy, so the owner has to run it.

`/private/tmp` is temporary storage. If the runtime worktree is removed
there, the API and worker cannot start again from it. Once the root checkout
is on `main`, the runtime can start from the root checkout again.

## Root-main runtime cutover — 2026-10-18

At 00:27 PDT the root checkout was on `main` at `8a3ef3c`, equal to
`origin/main`; the two modified documents and all 92 untracked files survived
the switch. The dashboard returned 200 for `localhost` and 403 for a hostile
Host header.

The single worker stopped cleanly on SIGTERM, followed by the API. Both were
restarted from the root checkout with the existing absolute storage, vault,
backup and `.env` paths: `make api` and `make workers n=4`. `/setup/status`
reported API, migration `c4e8a1b7d290`, all four workers, the vault and
`LLM_PROVIDER=ollama` as `ok`.

`lsof +D /private/tmp/jobrunner-main-runtime` returned no users, after which
that worktree was removed with `git worktree remove`. Other `/private/tmp`
worktrees, including PR #178's `/private/tmp/jobrunner-resume-style`, were
left in place.

## The owner's to do

- IMAP: set `IMAP_HOST`, `IMAP_USERNAME` and `IMAP_PASSWORD` (a Gmail app
  password) in `.env`, then restart the worker.
- Grade postings toward the 100 that ranking evaluation needs
  (`next_to_grade` / `grade_posting`, or the dashboard).
- Settle or keep the 5 uncertain fences on `/finish`.
- Decide where private files live, and when to provision the non-superuser
  role (`docs/harness-audit/database-role-migration.md`).
