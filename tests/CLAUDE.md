# Notes on what the gates prove, and the test database

Moved here from the root `CLAUDE.md` on 2026-10-11, word for word, so they
load only when work touches this directory. "§N" below is a section of
the root file, which keeps every heading and says where each section went.

## §15: what the gates do and do not prove

Every gate in §9 passes. Two of them pass against fixtures rather than the real
material their wording asks for, and that distinction is worth keeping visible
where the gates are defined rather than only in a test docstring.

- **Gate 5** asks for "a hand-labeled set of 20 postings — the ones you'd
  actually apply to". `tests/test_matching.py` has 20, written the way real
  postings read. They are not postings the owner labeled.
- **Gate 6** asks for "30 hand-labeled **real** recruiter emails".
  `tests/test_inbox.py` has 30 written to match. They are not real correspondence,
  and the cost of that is now measured rather than suspected. The fixtures were
  written beside the patterns that read them, so they use the phrasing the
  patterns expect: the fixture says "with other candidates" and matches, while
  "with another candidate" — the same sentence as recruiters write it — does
  not. Six of seven realistic rejection phrasings miss. On a rejection shaped
  like one that actually arrives, the rules abstain outright.

  `inbound_messages` in the owner's database is **0**. Gate 6 has never seen a
  real email.

Both suites are worth having — they catch regressions. Neither answers the
question its gate was written to ask, which is whether the scoring and the
classifier work on *this owner's* material. Swap the fixtures for real data
before trusting either number.

- **Gate 3 now checks what it says it checks.** The wording is "every
  noun-phrase entity in output traces to the source résumé"; the guard matched
  on capitalization, acronyms and digits instead, which is a proxy that cannot
  see a lowercase claim. A rewrite adding "with machine learning" to a résumé
  that never says it was accepted with **zero entities checked**.
  `packages/tailor/chunk.py` POS-tags the output and extracts noun phrases, so
  nouns and their adjectives are claims while verbs and adverbs stay free —
  §2.1 permits rephrasing, and "oversaw" for "maintained" is rephrasing.

  The tagger data is a download, not a package: `make nltk-data`. Without it
  the guard falls back to the old proxy, `GuardReport.extractor` records which
  one ran, and `make doctor` reports it. A guard that quietly loses a check is
  worse than one that never had it, because nobody re-reads a green test.
- **Gate 5's ranking half is now measured rather than asserted.** The gate
  checks one bit — ten wanted postings in the top ten — which cannot see a
  match slide from rank 1 to rank 10 and cannot compare two scorers. It also
  turns out to prove less than it looks: on those twenty postings the shipped
  scorer and a five-line token-overlap baseline both score a perfect NDCG@10,
  because the negatives are pastry chefs and truck drivers and every scorer
  separates those. `make bench-matching` reports NDCG/MAP/MRR/P@K with
  bootstrap intervals against a constant control, over the same labels plus
  twelve adjacent roles that actually discriminate. On those twelve NDCG@5
  falls to 0.577 and the control is statistically tied with everything, which
  is where the matcher's real weakness lives. The labels are still
  fixture-grade, so the harness refuses to report a production candidate at
  all — `docs/ML_EVALUATION.md` says what would have to arrive first.

  Two side findings worth keeping. `seeds/labeled_matches.yaml` is now the one
  definition of the Gate 5 set, which `tests/test_matching.py` reads by tag —
  two copies of a labeled corpus drift, and the gate was the copy that would
  go stale. And `filters.seniority_ok` passes everything when
  `target_seniority` is unset, which no production caller sets: a Junior
  Backend Engineer with a perfect technology match ranks in the top ten, and
  arming the target takes P@10 from 0.900 to 1.000. Whether to arm it by
  default is a separate decision; it now has a number attached.

- **Gate 1's HAR replay** is now real. `tests/test_greenhouse_har.py` replays
  bytes Greenhouse actually served, offline, and runs in `make gate-1` and in
  CI. It exists because the hand-written fixture beside it had native
  `<select>` while the live board had moved to react-select, so the adapter
  misread every dropdown and the suite stayed green. Re-record with
  `python -m scripts.record_har <posting-url>`. A fixture drifts silently; a
  recording only goes stale, and stale is visible.

- **Gate 1's live half** has never completed. `make gate-1-live` reaches
  `parse_posting` and `enumerate_fields` against real Greenhouse boards and is
  then blocked by a captcha at `fill`. Per §2.5 that is the correct outcome, not
  a bug — but it means the fill path is proven only against a fixture.
- **Gate 2** needs a real posting and a real profile by definition.
  `make gate-2` checks the offline half; `make gate-2-live` is the other half.

- **The Workday extractor's payload shape has never been seen.**
  `packages/crawler/workday.py` reads a board across pages, and
  `tests/test_workday.py` covers the pagination, the failure handling and the
  truncation rule — all of which are testable offline and all of which are the
  risk specific to a multi-request board. What is *not* established is that
  `title`, `externalPath`, `locationsText` and `bulletFields` are the keys a
  real tenant sends: the machine it was written on has no egress to
  `*.myworkdayjobs.com`, so the fixture was written from the API's documented
  shape. That is the same artifact as the pre-HAR Greenhouse fixture above,
  with the same failure available to it. Run
  `python -m scripts.record_workday <careers-url>` and compare before adding a
  Workday row to the registry.

  Two consequences of Workday itself, neither a defect to fix:

  - A tenant is its own host, so §2.6's 60s floor applies **per page**. A
    400-role employer is 20 pages, so 20 minutes for one company per cycle.
    `MAX_PAGES` bounds it at an hour and marks the board truncated, and a
    truncated board closes nothing.
  - The listing carries no descriptions — those are one request per posting,
    which at 60s is eight hours for a 500-role employer. So Workday postings
    have no `description_raw`, `embed_postings` skips them, and they score on
    title alone.

  This is an **extractor, not an adapter**. §11 still holds: nothing fills or
  submits a Workday form, and an application to one fails as
  `unsupported_site`.

### Closing the gate gaps §15 had only described

Four of the gaps recorded above were fixable without new data. They are, and
this section says what changed and what deliberately did not.

**Gate 3 now runs the rest of Phase 3.** The gate tested fabrication and the
PDF round trip; the cover letter and the tailoring cache were built, tested,
and never run by it. `make gate-3` now also runs `test_cover_letter`,
`test_apply_cover_letter`, `test_tailor_cache` and `test_apply_uploads_tailored`
— 170 tests rather than the fabrication suite alone. The last of those is in
for the reason the others are: it asserts the tailored file is the one that
reaches the employer, which is the defect that survived a green Gate 3 for as
long as nothing checked it.

**Gate 6's rules read English now, not the fixtures.** The rejection pattern
was written beside the corpus that exercises it, so it matched "with other
candidates" and missed "with another candidate" — the same sentence as
recruiters write it. Measured against fourteen phrasings taken from how
rejections are actually worded, **it caught 2**. It now catches 14, and the
labeled 30 are unchanged at 29 correct, 0 wrong.

Two things made that safe to widen. Rejection is matched *first*, so a loose
pattern there mis-files everything else — every new alternative therefore names
an outcome ("not successful", "has been filled") or somebody else getting it
("forward with another"), never a bare verb; "moving forward with your
application" is an interview and stays one, and a test holds that line. And it
matters most where there is no model: Ollama is not always running, Bayes reads
the realistic rejection as `interview` at a margin of 0.073 and is correctly
refused, so before this the chain resolved to `noise` and the application sat
in the tracker looking live.

**The seniority filter is reachable from a real run.** `filters.seniority_ok`
could always reject a rung mismatch and never did: no production caller set
`target_seniority`, so it defaulted to None and returned True in every crawl,
discover and rescore. It was reachable only from the benchmark — which is
where the number comes from: arming it takes P@10 from 0.900 to 1.000 on the
Gate 5 set, and the posting it removes is a Junior Backend Engineer with an
excellent technology match, exactly what a cosine cannot refuse on its own.

`profiles.target_seniority` is the owner's statement of their rung, and the
fallback lives in `apply_filters` rather than `score_and_store` so every caller
of the hard filters gets it. **NULL means "do not filter on level"**, which is
what every existing row gets and what the migration deliberately does not
backfill: §1 keeps a search filter separate from the profile's description of
the applicant, and inferring a rung from a résumé would narrow the feed on an
inference nobody made. It is a `SeniorityLevel` enum rather than a string
because a typo does not raise — `seniority_ok` passes everything for a target
it cannot place in the ladder, so `"Senior"` would read as "no preference" and
the feed would look unfiltered with nothing to explain it.

**Validation records itself.** `make validate-seeds` printed its verdict and
stopped, so which entries anyone had checked lived in a terminal scrollback.
`--write` (or `make validate-seeds-write`) stamps `checked` and `state` on each
entry and moves a dead board into `retired:` **with the statuses that condemned
it, rather than deleting it** — which is what this file has claimed happened
since the first sweep and never did. A slug that 404s today may be a rename
rather than a departure, and the evidence is what tells those apart later.
`load_seed` reads `companies:` only, so a retired board is not polled. The file
now answers "how many have never been checked" on its own: today, **119 of
119**, because the stamp is new even for the 29 that were swept.

### What is still not fixed, and why

- **Gate 1's live half is refused, not pending.** It reaches the form and is
  blocked by a captcha. §2.5 makes captcha-solving a hard scope boundary, so
  making this gate pass *is* the prohibited thing. It should stay red forever;
  what it proves is that the refusal works.
- **Gate 2's live half.** Needs a real posting reached over the network, and
  a real profile. `make gate-2-live URL=...` is the other half.
- **The 90 unvalidated boards.** `--write` exists; the sweep needs network
  egress and has to be run from the owner's machine.

### Getting real data in, for the two gates that need it

Gate 5's labels and Gate 6's emails cannot be written — that is the whole
point of §15. What *was* missing is the path from the owner's actual material
to the number, and both ends of it existed with nothing joining them.

**Gate 5: the judgements were already being collected.** `Provenance.OWNER`
and `FEEDBACK` were defined when `labels.py` was written and nothing ever
produced one, so every label in the repo is a `FIXTURE`. Meanwhile `/swipe`
has been writing `Match.decision` on every yes and no — which *is* a relevance
judgement about a real posting. `make export-labels` reads them back out as a
`LabeledSet` the benchmark can run on.

It exports as `FEEDBACK`, not `OWNER`, and `packages/matching/feedback.py`
says why at length. Two limits are load-bearing. A swipe is **binary**, so it
can only produce relevance 0 or 2 — it cannot express the gap between "would
apply" and "would drop everything for", which is exactly the gap NDCG's
`2**rel` gain exists to reward. And a swipe is **taken in feed order**, so it
is only ever recorded for postings the ranker already surfaced: nothing it
buried is ever labeled, and the model ends up graded on its own shortlist.
Real judgements about real postings, and not interchangeable with graded ones.

**Gate 6: `make import-mail src=<mbox|eml|dir>`.** Reads a Gmail export into a
worksheet — one row per message, the classifier's guess beside an **empty**
label field — and `make score-mail ws=...` reports accuracy over the rows the
owner filled in.

`guess` and `label` are deliberately separate fields, and a row with no label
is excluded from scoring entirely. Collapsing them would let the classifier
grade its own homework: accuracy would be 100% by construction, which is the
self-evaluating referee §45 and the master spec both refuse.
`test_the_worksheet_leaves_every_label_empty` holds that line.

The worksheet stores sender, subject, body and date and nothing else. It is a
second copy of other people's writing about the owner (§14), so it holds the
minimum a classification decision needs.

Neither of these produces a number on its own. They turn "this gate needs data
that does not exist" into two commands and an afternoon.

### Two failing tests that no commit had broken

Worth recording because the time went on the wrong suspect, and because the
failure is available to anyone working on this repo.

`tests/test_worker_host_blocking.py` failed twice in a full run —
`assert 0 == 1` on a posting count, in the test that measures the scheduling
defect — and passed in isolation, passed in a 1,078-test slice, and passed in
the next full run. The captured logs said what had actually happened:

```text
task_failed error=DBAPIError kind=crawl_company will_retry=True
StaleDataError: UPDATE on 'queue_tasks' expected to update 1 row(s); 0 matched
crawl_company_gone company_id=9baa8b41-…
```

Rows the test had **committed seconds earlier** were gone: the queue task it
was holding, and the company it had created. Nothing in the tree deletes a
`Company` except `conftest`'s truncate, and the session fixture there does more
than truncate — it **drops and recreates the whole schema** on startup, which
is right for a suite that owns its database.

A second `pytest` had been started while the full run was in flight — one file,
to check an unrelated fix. Its session fixture dropped the schema under the
running suite. The product was not involved at any point, and the first
hypothesis — that the new per-request metering was exhausting the connection
pool — was wrong in a way that would have cost a day to disprove by reading
code.

The fix is a claim rather than any amount of per-test defensiveness: the
session fixture takes a Postgres **advisory lock** on the test database and
keeps it for the run, so a second process is refused in 0.2s with the reason
and the remedy instead of silently wrecking the first. No test survives its
tables being dropped, so there is nothing to harden at that level.

Four details:

- **Refused, never skipped.** An absent database is a skip on purpose —
  `pytest` is green on a fresh checkout before `docker compose up`. A database
  already in use is a mistake with a remedy, and skipping 2,500 tests would
  hide it exactly as well as the collision did. `REQUIRE_DB` does not get a
  vote.
- **Session-level, not transaction-level.** The claiming connection commits
  immediately — the drop that follows waits on table locks and the claim must
  not be holding any — and a `pg_advisory_xact_lock` would be released by that
  commit, leaving a guard that reads like one and is not.
- **Released by closing the connection**, so an interrupted or crashed run
  leaves nothing to clear by hand. That matters because the fixture's *other*
  failure mode is already "a previous run left locks behind".
- **Scoped to one database.** Two suites against two databases still run at
  once; `TEST_DATABASE_URL` is how you do that, and the refusal message says
  so.

`tests/test_one_run_per_database.py` asserts the claim is held *while the suite
is running* — from inside the run, on a second connection — rather than that it
was taken once at startup.

### Two tests were reserving in the owner's live database

Found on 2026-10-08 while fixing a flaky test, by a mutation that changed
nothing. With the shared limiter's floor halved in code, four fetchers in
`tests/test_shared_ratelimit.py` were still two seconds apart.

Most code is handed its session by a fixture. A limiter built the ordinary
way, by `build_fetcher()`, takes one for itself from `packages.core.db`, and
that is bound to `DATABASE_URL`. Only the `client` fixture replaced it. The
two tests that go through `build_fetcher` used neither, so they reserved in
the owner's real `crawler_host_budgets` (in CI, in the migrated database)
while the fixture beside them cleared the test one:

```text
limiter's database : localhost:5433/jobrunner
fixture's database : localhost:5433/jobrunner_test
```

The live row for `boards-api.greenhouse.io` already held a 2s delay, and the
statement takes the greater of the stored delay and the one it is given. So
those two tests could not see the floor change, and every run of the suite
pushed the live crawler's next Greenhouse slot out by a few seconds.

How far it went was measured, not assumed: with `DATABASE_URL` pointed at a
database that does not exist, 3,575 tests passed and those two failed.

Two changes:

- **`tests/conftest.py` names the test database as the configured one**,
  before anything reads the settings. Whatever takes a session for itself now
  gets the test database, in every test. `alembic check` and the app are
  other processes and still read the real variable.
  `tests/test_the_suite_owns_its_database.py` holds it, with the case that
  leaked run unpatched.
- **The two tests bind the limiter's sessions to the test database by
  name**, so they do not rest on an engine cached across tests.

The flake itself was a second thing. The four-fetcher test asserted the gap
between each pair of wake-ups, and a gap between two wake-ups is the floor
plus how late the second was, less how late the first was. One request
leaving 114ms late on a CI runner read as the next leaving early (1.886s
against 2.0s with 5% allowed). It now asserts when each request went, counted
from the start: the second no sooner than one floor, the third two, the
fourth three. Being late only moves a request later, and every fault the test
is for moves one earlier. Simulated with one request held 300ms: gaps of
2.307, 1.691 and 2.0, the old check fails and the new one passes. With the
floor halved, the new one fails, which it could not do before either change.

What that leaves true of the limiter: it spaces the slots by the floor, and a
request that leaves late lands closer to the next one than the floor by
however late it was.

Not cleaned up: the live table holds eleven rows for made-up hosts
(`h0.example`, `acme.example`, `careers.slowsite.test` and others) dated
2026-09-14, from an earlier version of the suite with the same leak. They
are no host the crawler asks, and they are the owner's table to tidy.
