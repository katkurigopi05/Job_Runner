# Putting the targeted revision behind apply and the nightly batch

A plan for review, written 2026-10-13. **Nothing here is built.** Applications
and the nightly batch still rewrite every bullet, and
`python -m scripts.tailor_targeted` is the only caller of
`packages/tailor/targeted.py`. This says what switching them would take, so
the decision can be made on the particulars.

## What calls the ordinary rewrite today

| Caller | Where | What it does with the result |
|---|---|---|
| Apply | `apps/worker/apply_job.py::_tailor` | Sets `application.tailored_resume_id`. Tried in order: the owner's pin, a résumé the batch prepared, the cache, a fresh rewrite. Publishes even when every rewrite was refused, because the document still gains the rebuilt Projects section. |
| Nightly batch | `packages/tailor/batch.py::run` | Sets `match.tailored_resume_id`. Stops while `QUOTA_MARGIN + len(bullets)` calls remain. A provider failure leaves the posting pending. Every rewrite refused and no project to add: stores nothing. |
| Compare models | `packages/tailor/compare.py` | Two columns, each cached per provider. The owner picks one and the pick is pinned. |
| Offline | `packages/tailor/evaluate.py`, `scripts/bench_tailor.py` | Measure the rewriter. No rows. |

All four call `rewrite.tailor_bullets(provider, bullets, posting, corpus)` and
get a `TailorResult`. `targeted.revise` returns the same type, one entry per
bullet, which is what makes this a small change.

## The switch

One setting, `TAILOR_MODE`, `rewrite` (today, and the default) or `targeted`,
read in one new function that the three writing callers share, so they cannot
disagree about which document a posting gets:

```python
# packages/tailor/dispatch.py
async def tailor(provider_factory, parsed, posting_text, projects, corpus) -> Tailored
```

`Tailored` carries the `TailorResult`, the mode, and for a targeted run the
plan. It takes a provider *factory* because a targeted run with nothing to
gain must not build a provider at all.

## The six questions

**1. Nothing to gain.** `plan.targets` is empty for most postings (one bullet
of sixteen for the posting this was built on). No provider is built and no
call is made.

- Apply: with a relevant GitHub project, publish as today, since the Projects
  section is still worth sending; `resume_diff` says `changed: 0` and carries
  "no supported keyword gain available". With none, attach nothing:
  `_resume_path` already uploads the base and logs that it did.
- Batch: with no project either, skip the posting and report it as nothing to
  gain, not as failed. It stays "pending" by the batch's own test
  (`tailored_resume_id IS NULL`) and is planned again the next night, which
  costs milliseconds and no call. No new column.
- The batch's stop rule uses `plan.calls_needed` in place of `len(bullets)`.
  Sixteen calls a posting is what limits a night today. Planning costs none.

**2. A refused revision.** Per bullet, as now: the bullet is the résumé's own.
One repair, then no more. A provider that fails on a targeted bullet is a
provider failure exactly as today: the batch leaves the posting pending, and
apply goes on with the base bullets and shows the count.

**3. The cache.** `cache.tailoring_key` hashes the source résumé, the
posting's content hash, `TAILOR_SYSTEM.digest`, `TAILORING_REVISION`, the
provider, the model and the project ids. A targeted document must never be
served as an ordinary one or the reverse, so the key gains the mode, and for
`targeted` uses `TAILOR_TARGETED_SYSTEM.digest` and a digest of
`practices.PRACTICES`: adding a row changes what may be written and has to
invalidate what was written without it. Keys for `rewrite` are computed
exactly as now, so every cached row stays valid. A posting with nothing to
gain is cached too, under a key of its own, so apply does not re-plan it.

**4. Which document is attached.** The order does not change: the owner's
pin, then a batch-prepared résumé, then the cache, then a fresh run. What is
new is one refusal. `verify.check_pdf` reads the published PDF, and a PDF
that lacks a term the revision added, a figure or a listed technology is not
attached: the base is uploaded and the reason logged, the way a missing
tailored file is handled now. A fall in a heuristic score does not block
attachment. It goes on the review screen, where the owner is already
deciding.

**5. Going back.** Set `TAILOR_MODE=rewrite`. Nothing migrates. Targeted
rows are not served to the ordinary mode, because the key differs. A parked
application that already has a targeted résumé keeps it until the owner
re-tailors, edits or picks another column, which is what the pin is for. No
schema change in either direction.

**6. Tests this needs before it ships.**

- `tests/test_tailor_dispatch.py`: each mode returns one entry per bullet;
  `targeted` with nothing to gain never calls the factory.
- `tests/test_tailor_cache.py`: the two modes never share a key; a new
  practice row changes the targeted key and not the ordinary one.
- `tests/test_batch_tailor.py`: nothing to gain is not a failure and spends
  no call; the stop rule counts `calls_needed`; a second night re-plans
  without a call.
- `tests/test_apply_uploads_tailored.py`: a PDF that fails `check_pdf` is not
  the file uploaded, and the fallback is logged.
- `tests/test_apply_review_payload.py`: `resume_diff` carries the mode, the
  terms gained and the PDF check, and the TypeScript type has them
  (`tests/test_dashboard_vocabulary.py` is the pattern).
- `tests/test_review_resume_edit.py`: the owner's pin still outranks a
  targeted run, under both modes.
- `tests/test_tailoring_compare.py`: a third column, below.

## A smaller first step

Add `targeted` as a third column of **Compare models** before touching the
default. It runs only when the owner asks, costs two calls or none, is
guard-checked before it is shown like the other two, and makes the choice per
application with both documents on the screen. That is the way OpenRouter was
evaluated before anything tailored with it. The default is then switched, or
not, on what those comparisons showed.

## What is the owner's to decide

1. Whether to switch the default at all, or stop at the third column.
2. With nothing to gain and no project to add: attach nothing (above), or
   keep publishing a document identical to the base so every application
   has a "tailored" row.
3. Whether any heuristic fall should block attachment. The plan says none
   does.
