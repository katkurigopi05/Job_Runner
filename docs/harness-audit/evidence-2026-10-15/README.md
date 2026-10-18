# Audit evidence — 2026-10-15

Captured against checkout `87487bc`. See [audit](../../HARNESS_AUDIT_2026-10-15.md) for interpretation and limitations. These files contain aggregate observations and synthetic cases, not production résumé/mail/secret content.

| File | Evidence / execution boundary |
|---|---|
| `runtime.jsonl` | Live SELECT-only aggregates/config allowlist, query plan/timing, local GET health/Host probes. Server read-only transaction, 15s statement timeout; no embedding/model inference. Queries share a read-only transaction at default isolation, not a claimed immutable repeatable-read snapshot. |
| `read-only-probe.py` | Exact probe source. Resolves credentials internally and never intentionally prints them. Its `audit.unchanged` check surrounds reading the existing audit file, not the entire test session. |
| `tool-schemas.json` | All 35 registered tool schemas/metadata from FastMCP `list_tools`; no tool executed to obtain schemas. |
| `mcp-probes.py`, `mcp-probes.jsonl` | Actual dispatcher with injected fake API clients. Records ownerless approval/crawl forwarding and error flag. No HTTP connection/employer contact. Script writes schema snapshot under `/private/tmp`. |
| `worker-probes.py` | Three synthetic regression assertions that **fail on audited code**. Two use the existing test DB fixture; one intercepts failure persistence with a synthetic sentinel. Stored outside normal test discovery deliberately. |

## Test runs

Both runs set `REQUIRE_DB=1`. Shared fixture enforces distinct connection target, positive database marker and ownership advisory lock before schema setup. Existing fixture configuration stubs model calls and isolates artifact/audit paths. No new database was created.

Run 1: **395 passed, zero skipped, 101.53s**:

```text
tests/test_submission_ledger.py tests/test_submission_reconciliation.py
tests/test_review_is_decided_once.py tests/test_the_suite_owns_its_database.py
tests/test_the_suite_refuses_the_live_database.py tests/test_one_run_per_database.py
tests/test_red_team.py tests/test_mcp.py tests/test_mcp_ids.py tests/test_mcp_status.py
tests/test_mcp_grading.py tests/test_mcp_tracking.py tests/test_crawl_status.py
tests/test_worker_pool_connections.py tests/test_chat_retrieval.py
tests/test_retrieval_area.py tests/test_chat_seniority.py tests/test_chat_sponsorship.py
tests/test_chat_memory.py tests/test_chunk_model_stamp.py tests/test_audit_attribution.py
```

Run 2: **371 passed, zero skipped, 21.14s**:

```text
tests/test_no_fabrication.py tests/test_answers.py tests/test_llm_router.py
tests/test_request_budget.py tests/test_robots_rules.py tests/test_ratelimit_concurrency.py
tests/test_shared_ratelimit.py tests/test_chat_grounding.py tests/test_inbox.py
tests/test_inbox_duplicates.py tests/test_inbox_inference.py tests/test_notify.py
tests/test_notify_reparks.py tests/test_status_stream.py
```

Worker probes: **three assertion failures, 1.41s**, each confirming a documented defect; not part of the 766 passing tests. To reproduce without installing failing tests into the product suite, copy `worker-probes.py` to a temporary directory as `test_boundaries.py`, add `conftest.py` containing `pytest_plugins = ["tests.conftest"]`, then from repository root run:

```sh
REQUIRE_DB=1 PYTHONPATH=. .venv/bin/python -m pytest -q -rs -c pyproject.toml /private/tmp/jobrunner_audit_cases/test_boundaries.py
```

The actual temporary directory used was `/private/tmp/jobrunner_audit_cases`. Do not bypass fixture ownership safeguards. Reproduction mutates only synthetic test state. The raw sentinel is explicitly synthetic, not a real secret. Stale-claim probe updates authoritative owner/attempt while retaining the stale ORM object; a future fix also needs two-session concurrency tests.

Local raw logs remain in `/private/tmp/jobrunner-audit-{tests,adjacent,probes}-20261015.log` and run-1 JUnit in `/private/tmp/jobrunner-audit-tests-20261015.xml`. They are not copied into the repository because failure traces can expose test-environment configuration. The safe result summaries above are the durable audit artifact.

Host-only read checks additionally established loopback listeners on 5433/8000/3001, 16 GiB RAM/10 logical CPUs, ~68 GiB host filesystem availability, directory sizes and Desktop iCloud file-provider metadata. No provider/account identifiers or raw process arguments were retained. No per-container resource profile was obtained.

Restore/migration tests requiring disposable DB creation, full suite, live model calls, real-mail processing, live browser submission, model quality evaluations and UI disconnect rendering were not run. Framework experiments were not installed or promoted.
