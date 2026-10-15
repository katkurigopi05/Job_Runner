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


### A retried apply task can submit the same application twice (defect #16)

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
2. `find_existing` prefers another application's row to this one's own, whatever
   their ages. Read earliest-first, an older `released` row of its own won, and
   the application clicked for a job another listing of it had already sent.
3. An existing fence from another application parks at `needs_review` with conflict
   keys (`submission_conflict_application_id`, etc.). This application's own fence,
   once `submitted`, parks naming the application the job was sent through
   (`submission_recorded_application_id`, read by `submissions.sent_through`), so
   it is never left `running`.
4. This application's own fence in `attempting`/`uncertain` parks at `needs_review`
   with `submission_uncertain=True` without clicking.
5. The fence is made durable as `attempting` (or re-arms a `released` fence) and
   committed to PostgreSQL *before* `adapter.submit(page)` is called.
6. Pre-click refusals (`ManualCompletionRequired` on captcha, `SiteError` on missing
   button) restore the fence to its prior state (`released` or deleted), commit,
   and re-raise so the task fails without an uncertain fence.
7. If `adapter.submit` succeeds, the fence is set to `submitted`, transitioned to
   `ApplicationStatus.SUBMITTED`, and committed *immediately*. Only then is the
   screenshot captured. Screenshot errors are logged and swallowed; they must never
   undo the submitted status.
8. If post-click `adapter.submit` raises any other `Exception` or returns
   `submitted=False`, the fence is set to `uncertain`, parked at `needs_review`,
   committed, and returned normally (not re-raised, so the task is not retried).
9. Post-click networkidle timeouts must NOT be caught in `packages/ats/form.py`
   to invent success: an unknown outcome after the click is genuinely uncertain
   and must stop for human review.
10. Approval of an application with an uncertain fence sets the fence to `released`,
    allowing exactly one retry attempt. `_run_pipeline` clears `submission_uncertain`
    and earlier attempt keys on retry so later parks (e.g. questions) do not retain
    stale uncertainty.
11. Manual submission confirms the application's own fence if it has one, and
    otherwise the job's, as `submitted` with `source="owner"`, and records
    `confirmed_fence_application_id` in the `ApplicationEvent` transition payload
    so automated workers will not re-submit the same job.
12. Approval reads the ledger as it is now (`submissions.blocking`), never what a
    park saved: only a new run clears that, and a new run is what is being
    refused. Another application's row, or its own once `submitted`, refuses
    with 409 `invalid_state` and the ways out (reject; mark submitted; split the
    posting out of its group when that is how it matched). Read from what the
    park saved, a split that ended a conflict was refused for ever, and an
    already-submitted park was approved, re-ran and parked again for ever.
    The route takes the candidate lock before the row lock, as the worker does.

13. **`submitted=True` means the site confirmed it** (2026-10-15). It was
    `True` for any click: `submit_form` clicked, waited for the network, and
    read a confirmation if there happened to be one. A validation error, or
    a challenge that appears only on submit, was recorded as sent and fenced
    the job `submitted` for good. Reproduced on all four adapters' fixture
    forms. Now a click counts only when it produced confirmation text that
    was not on the page before it, so a "thanks for your interest" banner on
    the form does not count. Anything less is `submitted=False`, which item 8
    parks as uncertain and logs as `submit_unconfirmed`. A site whose real
    confirmation the selectors miss also parks; **mark submitted** is the way
    out, and sending twice is the worse mistake. The ledger tests faked the
    adapter and the adapter tests never reached the ledger, so
    `test_a_click_the_site_did_not_confirm_is_fenced_uncertain` runs a real
    adapter on a real page through `_submit`.

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
