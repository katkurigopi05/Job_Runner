# Phase 3: status truth and reconciliation — report

Part of the plan in `docs/HARNESS_AUDIT_2026-10-14.md` (added by PR #164).
Based on `main`, independent of the Phase 1 and 2 PRs. Done on 2026-10-15.
Findings F.6, F.8, F.9, F.10 and F.11 of the audit.

## What changed

| Finding | Before | After | Held by |
|---|---|---|---|
| F.6 the spider and a dead worker | A `running` row left by a killed worker read as a crawl in progress for good: `running: true, stalled: false` with the worker gone two hours | A `running` row counts as held only until its lease lapses, the same test `queue.claim_task` reclaims by. Past it, the work is pending; with no live worker it reads `stalled` | `tests/test_crawl_status.py` (7 new cases) |
| F.6 handoffs | The status route counted the sweep task only, so the spider stopped at the first handoff to company tasks | Sweep, discovery and company tasks and their rate-limit waits all count; a sweep scheduled for later does not | Taken from an uncommitted rewrite found in the working tree (last edited 2026-10-14 15:12 PDT), as it was |
| F.8 unsettled submissions | 5 legacy `uncertain` fences. The 4 on `failed` applications showed nowhere, and each blocked its job for good | `/finish` lists them under "Check with the employer first", each with **it was sent** (the existing route) and **it was not sent** (new: removes the fence, keeps it in a `note` event). Refused mid-run and for a `submitted` row. Not an MCP tool | `tests/test_submission_reconciliation.py` |
| F.11 boards that stopped answering | 60 open postings on 8 failing boards looked like any other card | The feed carries `board_unanswered` and `board_last_answered_at`; the card says "board not answering since …, may have closed". Filters and closes nothing | `tests/test_matches_board_health.py` |
| F.10 `lease_lost` | `queue.renew_lease` said the caller should stop; `run.py` lets the handler finish | Decision recorded, code unchanged: the click is protected by `_submit`'s locked re-read and the ledger, and cancelling mid-query risks a poisoned pool connection | Docstring |

Live, read-only, after the change: `/finish` lists all 5 legacy fences, and 4
of the top 200 feed cards carry the board warning (last answered 2026-09-18
and 2026-10-01). **None of the 5 fences was settled.** Whether each one
reached the employer is the owner's to check.

## Owner actions

1. **Settle the 5 fences on `/finish`**: one `needs_review` (Cloudflare,
   Senior Machine Learning Engineer) and four `failed` from 2026-08-25..27,
   three of them at a captcha. Look for a confirmation email or the employer's
   portal, then press one of the two answers.
2. **Turn the doorbell on (F.9).** Nothing is configured, so `notify` writes
   to the log only, while 1 application has waited since 2026-08-27 and 3
   need finishing by hand. The local, free choice is one line in `.env`:
   `NOTIFY_BACKENDS=desktop`. `webhook` needs `NOTIFY_WEBHOOK_URL` and sends
   an id, a status, company, role and a localhost link off the machine
   (`packages/core/notify.py`). This session does not write `.env`.
3. Restart the worker pool when ready (`make workers n=…`). It has been
   stopped since 2026-10-15 07:42 UTC.

## Not done

- Worker ids follow `socket.gethostname()`, which changed with the network
  (`Gopis-MacBook-Pro.local-N`, then `Mac.attlocal.net-N`), so
  `reclaimed_from_self` can be wrong. It is logged and decides nothing.
- The assistant's context does not yet mention a failing board. The feed
  does.
