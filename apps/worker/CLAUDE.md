# Notes on the worker and the apply pipeline

Moved here from the root `CLAUDE.md` on 2026-10-11, word for word, so they
load only when work touches this directory. "§N" below is a section of
the root file, which keeps every heading and says where each section went.

- The doorbell and the queue are in `packages/core/CLAUDE.md`; the forms themselves in `packages/ats/CLAUDE.md`.

## From §15: What the gates do and do not prove

### The tailored résumé was not the one being sent

Worth recording because every gate passed while it was true, and the code said
otherwise in a comment.

`apps/worker/apply_job.py` called `adapter.fill` — which uploads the file — and
*then* called `_tailor`. `_resume_path` read `profile.base_resume_id` and never
looked at `application.tailored_resume_id`. So Phase 3 ran in full on every
application: the rewriter, the guard, the project selection, the PDF, the diff
on the review screen — and the employer received the untailored base résumé.
The comment above the call claimed "the file the owner uploads is the document
the diff described", which is what a reader would have checked against.

Nothing failed, because no test asserted which path reaches the file input.
`tests/test_apply_uploads_tailored.py` now does, including the ordering itself:
`_tailor` must precede `adapter.fill` or there is nothing tailored to upload.

The fallback is deliberate and logged. If the tailored file is missing or
tailoring refused every rewrite, the base résumé is uploaded rather than none —
an honest untailored résumé beats an application with no résumé — but
`uploading_base_resume_tailored_file_unusable` is emitted, because sending the
base while the application record claims a tailored one is exactly how this
stayed invisible.

### The owner's choice at review was discarded on the way to the employer

The same defect as *The tailored résumé was not the one being sent*, one screen
further along, and it survived that fix because the fix asserted which résumé
`adapter.fill` uploads — not which résumé is still attached by the time it runs.

Approving a parked application does not resume mid-flight. It re-enters
`_run_pipeline` from the top: fresh page, re-`goto`, re-`enumerate_fields`,
re-`_tailor`. And every path in `_tailor` assigns `tailored_resume_id` — the
batch-prepared one, the cache hit, the fresh publish. So a decision the owner
made on the review screen was written to the row, and then quietly overwritten
by the run their approval started.

**Compare models** is where this had teeth. Choosing the cloud column set
`tailored_resume_id`; approving re-tailored with the router's *default*
provider, found that provider's cached document, and attached it instead. The
screen showed one document, the employer received another, and nothing on
either side reported the swap. §7 said "this sets the file an employer
receives", which is what a reader would have checked against.

`review_json["resume_pinned"]` is the fix, and `_tailor` returns before any of
the assigning paths when it is set. Two details are load-bearing:

- **It is checked against `tailored_resume_id`, not trusted alone.** If the row
  no longer points where the pin says, something else moved it, and re-tailoring
  is safer than uploading a document the application is no longer attached to.
- **The stored diff is marked, not cleared.** It is still the honest account of
  what tailoring did to the document the owner's version came from, and it
  carries the guard's refusal count — the one number that says whether the model
  kept trying to invent. What it must not do is go on reading as a description
  of the file about to be sent, so it gains `owner_pinned` and the panel says
  the changes describe the earlier document.

`tests/test_review_resume_edit.py` covers both ways of choosing. They are real
gates: with the pin removed, three go red and the log shows
`tailored_resume_published … version=3` landing on top of the owner's pick.


### A retried apply task can submit the same application twice (§16)

A database transaction rollback cannot undo the employer's Submit click.
Before the submission ledger, `_submit` called `adapter.submit(page)`, took a
screenshot, and called `transition(..., ApplicationStatus.SUBMITTED)`. Nothing
was committed until the task handler returned. A worker crash (OOM, SIGKILL,
power cut), browser timeout, or post-click exception rolled back the database
transaction. The queue lease expired, reclaimed the task, and resumed the
pipeline — which saw `owner_approved` and clicked Submit again.

The submission ledger (`applied_postings` table and `_submit` ordering) fixes
this and must not be undone:
1. `lock_candidate` advisory lock and `SELECT ... FOR UPDATE` re-read of the
   application row prevents concurrent submissions.
2. An existing fence from another application or a fence in `attempting`/`uncertain`
   parks at `needs_review` without clicking.
3. The fence is made durable as `attempting` and committed to PostgreSQL *before*
   `adapter.submit(page)` is called.
4. If `adapter.submit` succeeds, the fence is set to `submitted`, transitioned to
   `ApplicationStatus.SUBMITTED`, and committed *immediately*. Only then is the
   screenshot captured. Screenshot errors are logged and swallowed; they must never
   undo the submitted status.
5. If `adapter.submit` raises an `Exception` or returns `submitted=False`, the
   fence is set to `uncertain`, parked at `needs_review`, committed, and returned
   normally (not re-raised, so the task is not retried).
6. Post-click networkidle timeouts must NOT be caught in `packages/ats/form.py`
   to invent success: an unknown outcome after the click is genuinely uncertain
   and must stop for human review.
7. Approval of an application with an uncertain fence sets the fence to `released`,
   allowing exactly one retry attempt.
8. Manual submission records the fence as `submitted` so automated workers will
   not re-submit the same job.

Covered by `tests/test_submission_ledger.py`.

Rollout: stop/drain workers before `alembic upgrade head`, then restart all
workers on the same code. Old workers do not honor the ledger. The follow-up
backfill preserves submitted history and fences interrupted legacy runs as
uncertain (including claimed tasks whose application state rolled back).
Legacy timestamps are approximate, not recorded click times. Tracking-link
aliases of fenced jobs are rejected before a new application is queued.
Downgrading the backfill retains its evidence; dropping a nonempty ledger is
refused. Review uncertainty at the employer before approving another attempt;
if it was already sent, use **mark submitted**, not **approve & submit**.
