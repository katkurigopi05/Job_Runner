# Notes on the queue, the doorbell and what runs once

Moved here from the root `CLAUDE.md` on 2026-10-11, word for word, so they
load only when work touches this directory. "§N" below is a section of
the root file, which keeps every heading and says where each section went.

## From §15: What the gates do and do not prove

### Nothing told the owner an application was waiting

Three states park and all three were silent: `needs_review` wants an approval,
`needs_otp` wants a code, and `failed[manual_completion_required]` wants the
owner to finish the form by hand. The status was recorded and the dashboard
showed it, which on a queue whose entire promise is "nothing submits without
you" means the promise is kept only if the owner remembers to go and look.

`packages/core/notify.py` rings the doorbell. Three backends: `log` (the
shipped default, which goes nowhere), `desktop` (`notify-send` or `osascript`,
local and free), and `webhook` (the owner's own URL — ntfy, Telegram, Slack —
so a phone alert costs this project no dependency and no money).

**It is not a softening of §2.5.** The prompt that produced it recommended
integrating a CAPTCHA-solving service, which §2.5 and §11 both refuse and which
would also have been a paid API under §3. Nothing here defeats a challenge or
makes the browser look human. A blocked site still fails as
`manual_completion_required`; what changed is that the owner hears about it and
finishes it themselves, as a person, in their own browser. The work moved to a
human rather than around a control, and the notification links to the local
dashboard precisely so it cannot read as an automated retry.

Three details are load-bearing:

- **It fires after the commit, never inside `transition()`.** That function is
  the one place a status changes and would be the obvious hook, but it
  deliberately does not commit — a notification sent there announces an
  application that may still roll back. A doorbell that rings for work that did
  not happen is worse than a late one.
- **Idempotent on one park, not on the application.** The queue is
  at-least-once, so a re-run must not tell the owner twice; a `notified` event
  records delivery. Scoped to the park it announces — the boundary is the last
  transition into the current status — so an application that parks, resumes
  and parks again *does* ring again, that being a second thing to do. This
  said "keyed on the reason" and was wrong for as long as it said it; see
  *The doorbell was silent the second time* below.
- **A failed backend never fails the application.** Every delivery is wrapped
  and logged by name. The exception body is not logged, because a webhook error
  can echo the URL and the URL can carry a token.

The webhook payload is an id, a status, the company and role, and a localhost
link. Never the résumé, the answers, or the posting body — §2.8 permits one
third-party upload and a notification is not it. `test_the_payload_carries_no_application_material`
asserts the exact key set rather than the absence of any one field, so adding
one is a deliberate act.

### The doorbell was silent the second time

The same shape as the resent OTP below, one layer up: a check that promised to
be keyed on one thing and was keyed on something coarser, so the repeat it
existed to allow was swallowed.

`notify_if_parked`'s docstring — and §15's bullet above — both said "an
application that parks, resumes and parks again does ring twice". It asked
whether the application had **ever** been notified for this reason, with no
time bound, and `ParkReason` is derived from the status alone. Three values.
So the second park was silent whenever it parked the same way as the first,
which is the common case:

```text
park needs_review -> rings  -> owner approves -> re-runs
park needs_review -> SILENT
```

§6 has `needs_review ──approve──> running`, and §15 already records that
approving re-enters `_run_pipeline` from the top: fresh page, re-`goto`,
re-`enumerate_fields`. The `report.is_complete` park sits *above* the
`owner_approved` short-circuit in `apply_job._run_pipeline`, so a form still
carrying an unanswered required question parks again — and the owner, who
approved it and heard nothing more, has every reason to believe it was sent.
The OTP case is worse for having a deadline: the site asks for a second code
and nothing says so.

This is the promise in §2.3 failing quietly. "Nothing submits without you" is
kept by parking; the owner *finding out* is what makes parking different from
losing the application, and that is the whole job of this module.

`test_parking_a_second_time_rings_again` in `test_notify.py` looked like the
covering test and was not: it parks at `needs_review` and then at
`failed[manual_completion_required]` — two *different* reasons, so it only ever
exercised the case that already worked. The §15 pattern again, and the reason
the defect survived a green suite.

The boundary is now the last transition into the current status, and two
details are load-bearing:

- **A status reached without `transition()` keeps the old whole-history
  check.** There is no boundary to scope by, and the fallback can miss a
  re-ring but can never ring twice for one park. §6 requires every status
  change to go through `transition()`, so nothing in the pipeline reaches it.
- **`tests/test_notify_reparks.py` commits for real rather than using
  `db_session`.** `at` defaults to the transaction clock, so inside one
  transaction every event shares a timestamp and none of this is visible.
  `run.py` commits the task and *then* calls `notify_if_parked`, so in
  production the park and its notification always land in different
  transactions; the test reproduces that rather than assuming it. Reverting
  the fix turns two of its five red, and the other three pass either way —
  they pin the behaviour that was already correct.

## §24: discovery waits for everything else

Found on 2026-10-06 by starting a crawl and watching it. One tick queued 3,234
`discover_company` tasks and 682 `crawl_company` tasks with the same
`run_after`, and `claim_task` ordered by `run_after` alone. Five workers took
discovery, about 17 tasks a minute, and after two minutes no board had been
polled and no posting had arrived. At that rate the board polls, which are
what bring postings in, were three hours away.

`claim_task` takes `after_others`: kinds claimed only when nothing else is
runnable. The worker passes `YIELDING_KINDS`, which is `discover_company`.
After the restart, 134 boards were polled in the first 75 seconds and 227 new
postings stored.

Three things to keep:

- **It is not only about the crawl.** An approved application and the inbox
  poll are queue tasks too, and both would have waited behind the discovery
  backlog. Nothing was waiting that day, which is why it had not been seen.
- **Only runnable work is yielded to.** The next tick is always pending with a
  `run_after` a few minutes ahead. Yielding to that would leave discovery
  idle with nothing to wait for. `tests/test_queue.py` holds it.
- **Among discovery tasks the order is still `run_after`**, which is what
  `defer_task` moves when it hands a task back for a busy host.
- **`after_others` is ordered** (2026-10-19). Each kind in it also waits for
  the kinds before it, so `YIELDING_KINDS` is discovery, then the deep dive
  (`deep_dive_company`), which the owner asked for once everything else is
  done. `test_a_later_yielding_kind_waits_for_an_earlier_one`.

Seen the same day: `make workers` ignored both an interrupt and a terminate
for 20 seconds and had to be killed. The five tasks it was running kept their
leases for the five minutes those last. The cause was that every claimant
installed its own signal handler, and a loop keeps one handler per signal, the
last. A terminate stopped one claimant of five; on 2026-10-12 the other four
were still claiming 38 minutes later. `run_pool` now makes the one event its
signals set and hands it to every claimant
(`test_one_signal_stops_every_worker_in_the_pool` in
`tests/test_discovery.py`).

That fix was written on 2026-10-04, as `fix/worker-pool-stop`, and never
reached `main`: the branch was pushed and no pull request was opened. It was
found again on 2026-10-12 by fixing the same thing a second time, and the
earlier commit is the one that was taken.

Two more things from that day, when the owner asked for more claimants to get
through 3,900 discovery retries:

- **Discovery is slow because it waits, not because it works.** Five
  claimants finished 19 tasks a minute, and 86% of their time was the 60s
  wait before a second request to one company's site (§2.6), which a task
  holds its claimant for. Fifteen claimants finished about 50 a minute. The
  floor is per host and is reserved in the database, so more claimants do
  not move it.
- **A claimant needs two database connections**: one held for as long as its
  task runs, and a second for what runs beside it (the rate limiter's
  reservation, the request meter, a heartbeat). SQLAlchemy's own pool stops at
  15, so `DB_POOL_SIZE` and `DB_MAX_OVERFLOW` are settings, `make workers
  n=...` sets the first to two per claimant, and the worker refuses a count
  its pool cannot hold (`tests/test_worker_pool_connections.py`).

### One crawl at a time was a count, and two requests are two transactions

Found by review on 2026-10-08. `request_crawl` counted the crawl tasks
waiting and enqueued when there were none. `POST /crawl`, the assistant's
"run crawler" and `make crawl` are separate transactions, and a count cannot
see a row another transaction has not committed. Run with the second request
starting while the first was still uncommitted: two crawls queued. The
sweep's own reschedule, `_schedule_next_tick`, is the same count and insert
and did the same.

Both take a transaction-level advisory lock first (`_one_guard_at_a_time`),
so the second waits for the first to commit and then counts its row. Three
things to keep:

- **It rests on READ COMMITTED**, the engine's default, which gives the count
  a fresh snapshot after the wait. Under REPEATABLE READ the count would
  still not see the row.
- **A caller ends its transaction straight after.** The lock is held until
  then and the next request waits on it. All three callers commit or leave
  their session on the next line, and the worker commits when the handler
  returns.
- **Not a unique index.** A running sweep and its pending successor are two
  unfinished crawl tasks on purpose.

`tests/test_crawl_command.py` holds both on two connections, and reads
`pg_locks` to know the second is waiting where the inbox's race test sleeps.
With the lock taken out of one guard, that guard's test fails and the other's
passes.

## §29: a task's life after it is claimed

Added on 2026-10-19, after the owner compared the queue with a crawler-style
job system. Each was a gap that cost something:

- **Retries back off** (`RetryPolicy`). The wait after attempt n is
  `base * 2**(n-1)`, capped; it was a flat 60s for every kind and every
  attempt. The worker sets a policy per kind (`run.RETRY_POLICIES`): an
  employer's site and a board get minutes, discovery and the deep dive ten
  minutes doubling to six hours, and the crawl tick and inbox poll stay short
  because each keeps a loop going. `max_attempts` stays `DEFAULT_MAX_ATTEMPTS`
  for every kind: `claim_task` judges a crashed worker's task by that number,
  so a kind given more would lose the extra attempts to a crash.
- **A pending task can be cancelled** (`cancel_pending`; `POST
  /queue/tasks/{id}/cancel`, `POST /queue/cancel` for a whole kind). Only
  pending ones: a running task finishes or fails on its own. `apply`, `crawl`
  and `inbox` are refused: an application is withdrawn by rejecting it, and
  the crawl tick and inbox poll schedule their own next run, so cancelling one
  would stop crawling or mail for good. `cancelled` is a terminal status.
- **A handler that hangs is stopped** (`run.TIME_LIMITS`). The lease is
  renewed while a handler runs, so one that hung held its worker for ever.
  Past its kind's limit it is failed as `timed out after Ns` and retried like
  any failure. Limits sit well above the slowest honest run; every request
  already has a network timeout and this is the backstop. A handler's own
  `TimeoutError` is an ordinary failure: only `deadline.expired()` is a
  timeout.
- **A task records when it finished** (`finished_at`, set on done, failed for
  good and cancelled). With `locked_at`, which a claim sets and nothing
  clears, it is how long the last attempt took. `GET /queue/stats` reports
  counts, median and p95 durations, the oldest runnable wait and the latest
  error per kind. NULL on tasks finished before the column existed; nothing
  invents one.
- **Finished tasks are cleared** (`prune_finished`, hourly, on the first
  worker of each pool, under an advisory lock so two pools never prune at
  once). Done and cancelled after `QUEUE_KEEP_DONE_DAYS` (7), failed after
  `QUEUE_KEEP_FAILED_DAYS` (30). **The newest finished task of each kind is
  always kept**, because the crawl status reports when the last crawl
  finished. There were 63,436 finished tasks back to 2026-08-20.

`tests/test_queue_lifecycle.py` and `tests/test_queue_api.py`.

## §27: races

Races, each reproduced with real transactions and each held by a test that
failed first:

- **The approval gate locks the row it decides**
  (`routers/applications.py`, `with_for_update=True`). Three approvals at once
  were three runs queued, and an approval and a rejection at once were both
  accepted. With `make workers n=4`, two runs of one application is the form
  sent twice: `begin_work` takes a `running` row for an abandoned run.
  `tests/test_review_is_decided_once.py`.
- **A producer that looks before it enqueues takes a lock first.**
  `request_crawl` and `_schedule_next_tick` counted and then enqueued, in two
  statements. This was fixed twice, in parallel: here as `queue.decide_alone`,
  keyed per task kind, and on `main` as `crawl_job._one_guard_at_a_time`
  (§24). **`main`'s is the one in the tree** — one landed fix beats two, and
  a second guard nothing calls is the dead code §15 keeps recording. Either
  way it is a transaction-scoped advisory lock, so the caller must commit
  promptly, and not a unique index: a running tick and its pending successor
  are two unfinished crawl tasks on purpose.
- **A grade's offer belongs to one profile.** It was kept by posting alone
  and read twice — once for the form, once after the owner answered — so an
  offer for another profile in between took the grade. Fixed twice in
  parallel as well: here by reading the offer once (`_offered`), and on
  `main` by keying what the server remembers on `(profile, posting)` (§9).
  **`main`'s is the one in the tree**, and it is the stronger of the two:
  under protocol 2026-07-28 the resolver runs again once the form is
  answered, so reading once per call was never enough on its own. A
  consequence of taking it: the form does not name the profile, and the two
  tests here that pinned that wording are gone — `main`'s
  `test_a_request_made_while_the_form_is_open_does_not_move_the_grade` covers
  the race under both protocols.
