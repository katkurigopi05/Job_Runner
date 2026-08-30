---
description: Send the spider out — crawl the company registry for new job postings and report what it found.
---

Run a crawl of the company registry and tell the owner what turned up.

Arguments: `$ARGUMENTS` — pass `force` to re-emit postings whose content hash
is unchanged. Normally omit it: a second run emitting nothing is change
detection working, not a failure.

## Why this exists rather than just `make crawl`

`scripts/crawl.py` **enqueues** a task; it does not crawl. The worker owns the
browser and the per-host rate limiter, and a second crawler beside it would
poll the same hosts on a counter the worker cannot see — §2.6's floors are per
host, so two processes each honouring them independently honour neither.

The consequence is the failure that script was written about: an enqueue with
no worker draining it sits forever, and "no new postings" reads exactly like
"the sweep never happened". So check the worker *before* queueing, not after.

## Steps

1. **Database.** `pg_isready -h localhost -p 5432`. If it is down, start it —
   `make up` normally, or `sudo pg_ctlcluster 16 main start` where Postgres is
   installed directly rather than in Docker. Do not continue without it.

2. **Worker.** `pgrep -af "apps.worker.run"` is a hint, not an answer — the
   pattern can match the shell running the check, and then it always says yes.
   Treat a match as "probably", and confirm behaviourally in step 5: a task
   that is still `pending` with no `locked_by` after a minute means nothing is
   draining, whatever `pgrep` said.

   Start one before queueing if in doubt: `make worker`, or
   `.venv/bin/python -m apps.worker.run` in the background. An enqueue with no
   worker is the silent failure above, and starting a second worker is safe —
   the queue claims with `FOR UPDATE SKIP LOCKED`.

3. **Count first.** Record the posting count and the newest `first_seen_at`,
   so the report can say what *this* crawl added rather than what exists:

   ```sql
   select count(*), max(first_seen_at) from postings;
   ```

   An empty `companies` table is **not** a problem on a fresh database. The
   registry lives in `seeds/companies.yaml`; `crawl_job` loads it with
   `load_seed()` and `crawler/crawl.py` upserts a `Company` row per seed as it
   goes. The table is an output of the first crawl, not an input to it.

4. **Queue it.** `make crawl` — add `force=1` only if the owner asked. If the
   script reports a crawl already pending or running, do not queue another;
   wait for that one instead.

5. **Wait for it to drain**, checking rather than sleeping blindly:

   ```sql
   select status, count(*) from queue_tasks where kind = 'crawl' group by status;
   ```

   Poll every 15s or so. A registry sweep respects a 60s floor per host (2s for
   the shared ATS APIs in `ratelimit.SHARED_API_HOSTS`), so a full pass over the
   registry takes minutes, not seconds. Do not report failure early — a slow
   crawl is the rate limiter working.

   If the task is still `pending` with `locked_by` null after a minute, no
   worker is draining it. That is the real check; go back to step 2.

6. **Report.** New postings since step 3, and the companies they came from:

   ```sql
   select c.name, p.title, p.location
   from postings p join companies c on c.id = p.company_id
   where p.first_seen_at > :before
   order by p.first_seen_at desc limit 20;
   ```

   Then tell the owner where to look: `/matches` in the dashboard for scored
   matches, and `make rescore` if the résumé changed since these were scored.

## Reporting honestly

- **Zero new postings is a normal result**, not an error. Content-hash change
  detection means a second run over an unchanged registry emits nothing, and
  Gate 5 asserts exactly that. Say "nothing new since the last sweep" and give
  the timestamp of that sweep.
- **A crawl that found nothing at all, ever, is different** and worth flagging.
  Check `seeds/companies.yaml` has live entries and that `make validate-seeds`
  passes. 21 of the original 50 seeds returned 404 from both the board API and
  the rendered page and are kept at the bottom of that file with the evidence
  rather than deleted — a 404 board yields zero postings, which reads
  identically to "nothing new since the last poll".
- **Read the worker log before calling zero "nothing new".** The crawl logs a
  one-line summary — `N boards fetched, N postings emitted, N skipped, N
  failed`. A high `skipped` with `crawl_blocked` lines means the boards were
  never reached at all:

  ```
  crawl_blocked  company=Adyen  reason='robots.txt could not be read;
                                        refusing to assume the rules allow us'
  ```

  That is §2.6 refusing to guess when it cannot read the rules, and it is the
  correct behaviour — but it means *nothing was polled*, which is a different
  report from "polled everything, nothing changed". Seen for real on a machine
  behind an egress proxy: 119 skipped, 0 fetched, 0 failed. Check outbound
  network before looking for a crawler bug.

- **Never re-run back to back to get a better number.** §2.6 is a courtesy
  floor and two sweeps minutes apart poll the same hosts for nothing.
