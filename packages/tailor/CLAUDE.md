# Notes on résumé tailoring

Moved here from the root `CLAUDE.md` on 2026-10-11, word for word, so they
load only when work touches this directory. "§N" below is a section of
the root file, which keeps every heading and says where each section went.

## From §7: LLM provider abstraction

### Comparing two models on one posting

`/review` has a **Compare models** panel: it tailors the same posting with the
local model and with the cloud one and shows both, each with its rewrite count,
its guard-refusal count, and a button that makes it the document to upload.

It exists because §7 made the provider settable without making it *decidable*.
Answering "is the cloud one better for this résumé and this job" meant editing
`.env`, re-running, and holding the first result in your head.

Three properties are load-bearing:

- **On demand.** Each remote side is another §2.8 upload of the owner's résumé.
  Running both on every application would double that on every application,
  including the ones rejected at review. The tailoring cache is consulted per
  provider, so comparing a posting twice sends nothing.
- **Both sides are guard-checked before either is shown.** A comparison offers
  each column as something the owner may choose and send. An unvetted draft
  presented that way is a fabricated bullet with a button under it. The refusal
  counts are shown per side — a model that keeps trying to invent should not
  look identical to one that does not.
- **A side that cannot run is a reported column, not a missing one.** No key,
  spent allowance, Ollama not started: each becomes a candidate carrying the
  reason. A comparison silently missing half of itself reads as a verdict on the
  half that is there. `CannotCompare` is separate and covers the case where
  there was never anything to compare — no base résumé, no posting text —
  because a precondition is not a model outcome.

Selecting is restricted server-side to the two versions actually compared. This
sets the file an employer receives, and "any résumé id this candidate owns" is a
wider door than the screen needs.

**It did not always set that file.** Selecting wrote `tailored_resume_id` and
approving then re-entered `_tailor`, which reattached the default provider's
cached document over it — so the column the owner picked was shown here and the
other one was sent. The choice is pinned now; see §15, *The owner's choice at
review was discarded on the way to the employer*.

**The remote side is nameable per comparison.** `cloud` on the compare request
picks it for that run only; omitted, it is whatever real tailoring would use.

This exists because the default could not answer the question for OpenRouter.
§7 keeps `openrouter` out of `QUALITY_ORDER`, so the only way to make it the
cloud column was `LLM_TASK_TAILOR=openrouter` — which also redirects every real
tailoring call. The owner had to *adopt* a provider in order to evaluate it,
which is the friction this panel exists to remove, pointing the wrong way: the
provider hardest to name is the one whose output most deserves a look first.

It is not a back door into §7. Only a provider with a key configured may be
named, so pasting a key is still what makes a route reachable at all; nothing
routes to OpenRouter by default; and naming one here moves no setting, so the
next application tailors exactly as it did before. That is the same shape as
§14's per-question provider choice in `/chat`. The picker states where the
résumé goes for each option, and says outright that OpenRouter forwards to an
upstream it cannot name — §2.8 permits this upload, it does not excuse making
the recipient invisible at the moment of choosing.

**A provider that never answered is not a guard refusal.** Found on the first
real local-vs-cloud run. `tailor_bullet` keeps the original line for either
reason and set `rejected_reason` for both, so `summarize` counted them
together — and the comparison rendered "1 refused by the guard" for a model
that had returned a 404 and never written a word. On a screen whose entire
purpose is judging two models against each other, that is the wrong verdict
about the wrong subject: "the guard refused this" describes what a model tried
to write, and a transport error describes the network. `provider_failures` is
counted and displayed separately, and the column says so.

Tuning any of these against the guard's own pass rate is the trap in
`docs/REFERENCE.md` §3.6: it optimises the one referee we control, and a
rewrite can satisfy the guard while reading worse. Change one only with a
second measure reported beside it.

## From §15: What the gates do and do not prove

### The ATS score was reading the sales pitch

Found by running the guard and the ATS scorer over a rewritten résumé against
the real crawled postings in `tests/fixtures/golden/`, which is the first time
either had been pointed at one.

§7 makes the ATS keyword score the *independent second measure* — the referee
that is not the fabrication guard's own pass rate. On the Palantir Forward
Deployed Software Engineer posting it was measuring nothing. `job_terms` ranks
by frequency and keeps forty; in 5,912 characters of narrative, `Python`,
`Java`, `C++` and `TypeScript/JavaScript` appear exactly once each in a single
line naming the stack, while `world`, `problems`, `believe` and the company's
own name recur throughout. The top forty held every one of the latter and none
of the former, so a tailored résumé was being scored against a vocabulary
containing no skills at all — and the rewrite's measured gain was, correctly
and uselessly, zero.

The fix is in `ats._requirements_text`, which cuts to the posting's own
requirements section before the terms are counted. Across the twelve golden
postings, prose terms fell from **17% to 8%** of the vocabulary and the term
count *rose* (372 → 419): it is finding more real terms, not merely filtering.
On the worst posting it went 43% → 8%. One posting got worse (6% → 11%), which
is recorded rather than averaged away.

Two things about where the fix lives:

- **On the measurement side only.** `job_terms` output is also
  `TermReport.missing`, which `rewrite.vet` treats as vocabulary a rewrite may
  not borrow. Narrowing *that* removes words the model may then introduce
  unchallenged, so the guard rail is untouched and only the scorer's view of
  the posting changed. `ats.py` already drew that line; this stays behind it.
- **Headings rather than a technology list.** Ranking known technologies above
  prose was tried first and is worse twice over: a second skill vocabulary
  drifts against the alias table, and no list recognizes the skill it has not
  heard of — which is the term a posting is most worth reading for. All twelve
  golden postings carry a requirements heading. A posting without one falls
  back to being read whole, exactly as before.

### A Skills list moved technologies between employers

The same session, the same method. §2.1 says a rewrite may inject keywords
"already supported by that résumé entry **or a shared source section**", and it
separately forbids borrowing one entry's skill onto another. A Skills list makes
those two clauses contradict each other, and the second one was losing: every
technology named in Skills was indexed as shared, so it supported a claim on
*any* employer.

Scoped to an employer that never touched it, `Built Python services on
Kubernetes` passed while the Skills section existed and was correctly refused
when that section was deleted. Same claim, same scope, opposite verdicts,
decided by an unrelated part of the document.

`SourceCorpus.supports` now withdraws a shared token when some entry claims it.
The three cases, which are the whole design:

- **In this entry** — supported, as before.
- **In Skills only, claimed by no entry** — still supported anywhere. This is
  §2.1's shared-section allowance and it is deliberately untouched: a skill the
  owner lists but never attributed to a job can go anywhere.
- **In Skills *and* in another entry** — refused here. The résumé has already
  said where it belongs, and "the owner knows this" is not "the owner did this
  here".

On a two-job fixture it withdraws 13 of 34 shared tokens — the technologies —
and leaves 21, being contact details, education, and the skills the résumé
never attributed to anything. The full suite including Gate 3 passes unchanged,
so this refuses claims that were wrong rather than claims that were working.

### Phase 3 scope Gate 3 does not cover

§9 Phase 3 lists two things beyond what Gate 3 tests. Both are now built, and
both stay recorded here rather than being deleted: the gate still does not
cover either, so "Gate 3 passes" continues to mean less than "Phase 3 works".

- **Cover letter.** ~~Nothing calls it.~~ Wired. `apps/worker/apply_job.py::_cover_letter`
  writes one, `Application.cover_letter_ref` is written, and
  `tests/test_apply_cover_letter.py` asserts the wiring the module's own tests
  could not: that the letter is written *before* `adapter.fill`, and that it
  reaches the field. `tests/test_greenhouse.py` types it into the real fixture
  textarea, because a letter written, vetted, stored and then not typed is the
  same defect as the résumé that was never uploaded.

  Three things are deliberate. **The form is read first** — the questions
  decide whether a letter is written at all, because most postings never ask
  for one and a provider call for a field that does not exist buys nothing.
  **A refusal is recorded, not swallowed** — the guard offers no fallback
  here, so `review_json["cover_letter"]` carries the reason; a form that asked
  and got nothing otherwise looks identical to a form that never asked, and
  writing it by hand is the owner's call. **A resumed run reuses the stored
  letter** rather than writing a second one: any real provider returns
  different prose the second time, and the owner approved a specific letter.

  Wiring it surfaced a defect in the module that no test could see from
  inside. `cover.py` strips the greeting before sifting — `_GREETING` exists
  precisely so "Dear Hiring Manager," stops being read as a claim about a
  `Manager` — and then `write` joined it back on before calling `vet`, which
  runs the guard over the whole letter again. So the strip was undone one line
  later and the refusal came back. Every test in `test_cover_letter.py` built
  letters with no greeting, so all of them passed while the most common
  opening a model produces was refused on its first four words. `vet` now
  strips the addressing itself, which covers both callers. §2.2 and the length
  bound still judge the whole letter: the scaffolding is exempt from tracing
  to the résumé, not from the salary rule.
- **Caching of tailored versions.** Built — `packages/tailor/cache.py`. The
  condition it was waiting for arrived: `LLM_PROVIDER=gemini` with a key set,
  and tailoring text now leaves the machine. Keyed on the source résumé, the
  posting's `content_hash`, `TAILOR_SYSTEM.digest`, the attached project ids,
  and the provider and model — everything that changes the output, because a
  cache keyed on less serves a résumé written for a different job and nothing
  about it looks wrong. A posting with no `content_hash` is not cached at all
  rather than keyed on something weaker.

  Worth being honest about how much it currently saves: **very little.** With
  one profile, no two postings sharing a `content_hash`, `pending()` already
  skipping tailored matches, and `_prepared_resume` already reusing within an
  apply, there is no live path that re-tailors the same posting. It is
  insurance for when there are several profiles, and for a re-run after a
  partial failure.

  The measured duplication is somewhere else. The audit trail's heaviest day —
  204 uploads against a ceiling of 200 — carried only **69 distinct payloads**,
  and 189 of the 204 were `tailor.system`, and none of it persisted. That is
  `packages/tailor/evaluate.py`, an offline harness with no session, re-run
  over the same fixtures. This cache does not touch it and should not; a cache
  there is the follow-up that matches the evidence.

  This paragraph used to reach that conclusion by way of "the database holds
  one résumé and no tailored ones", which is no longer true: on **2026-09-12**
  it holds **6 résumés — 2 base, 4 tailored** — against 1 profile and 1
  candidate. The conclusion is unaffected, because what makes the cache
  near-useless today is the one profile and no two postings sharing a
  `content_hash`, not the row count. The sentence is corrected rather than left
  to be believed.

Gate 3 passed without either of them for as long as neither existed, because
it tests fabrication and the PDF round trip — the part with consequences — and
it still does not test either now that both are built. The cover letter has
its own tests rather than a gate. Recorded here so a green Gate 3 is not read
as more than it checks.

### Editing the résumé on the review screen

The review screen showed the attached document and could not change it. A
tailored bullet that read wrong left two options — reject the application, or
send it anyway — and editing the base on the résumés page did not help, because
a résumé already tailored for this posting is not the base.

`POST /applications/{id}/resume/edit` is the smaller path, and
`packages/tailor/revise.py` is the reason it is not a second implementation:
both edit routes render the PDF, rebuild `raw_lines`, and version rather than
mutate through one function. A second copy of that sequence is how one of them
ends up storing `parsed_json` with no file — invisible until an employer
receives the old PDF.

**Scoped to the application, and that is the whole design.** The edit lands on
`tailored_resume_id`; the profile's base moves only if the owner ticks the box.
On this screen the subject is one employer, and a résumé written for one posting
is a poor starting point for the next — adopting it silently would make every
future application inherit this job's phrasing from a screen that never
mentioned them. `ApplicationResumeEdit.adopt` defaults to `False` for that
reason, and is a separate schema from `ResumeEdit` precisely so the two
defaults can disagree.

Parked only. A `running` application is mid-fill in a browser and a `submitted`
one has already sent its file; editing either changes a screen without changing
anything an employer sees.

The guard is not applied to the owner's own edit, and that is deliberate — the
same reasoning `packages/tailor/edit.py` already records. §2.1 constrains the
*model*, not the owner writing their own history. `raw_lines` is rebuilt so a
fact the owner adds is source text for the next tailoring pass, rather than
something the guard refuses on their behalf.

**Over MCP the author is a model, so there the guard runs.** `edit.py`'s
reasoning turns on who is typing, and a tool call inverts that: an unguarded
résumé write handed to an assistant is exactly the door §2.1 closes — it would
let a model put an employer, a credential or a metric onto a document going to
a real employer under the owner's name, checked by nothing.

The API cannot tell a person from a model, so the caller declares it:
`ApplicationResumeEdit.guard`, off for the dashboard, and sent `true` by
`apps/mcp/server.py` with no parameter that could say otherwise. A settable
guard would be an opt-out from a non-negotiable by argument, which is the same
shape as the `.env` opt-out §7 refuses; `test_the_tool_cannot_turn_the_guard_off`
holds the tool's schema to it.

Only *added* lines are checked. The guard is strict enough that some genuine
source text does not survive a round trip, so re-judging carried-over lines
would refuse edits that changed nothing. The check is document-wide rather than
scoped to one employer's entry — an added line has no entry to be scoped
against yet — so cross-entry borrowing is not caught on this path and callers
should not assume it is. `packages/tailor/revise.py::guard_edit` says so at the
point it matters.

A refusal is not a verdict on the fact. If the owner says it is true, the answer
is for them to type it on `/review`, where the edit is theirs — not for an
assistant to reword it past the check. Both the tool docstring and
`docs/USAGE.md` say that, because it is the one place this design could
otherwise push a model toward laundering a claim.

`GET /applications/{id}/resume` exists for the same reason `revise.py` does:
"which résumé is attached" now has three readers — the review screen, the MCP
tools, and the uploader — and a client working it out for itself is how a screen
ends up describing a document other than the one being sent.

### What the first real comparison showed about the tailorer

The compare panel's first live run — llama3.1 against openrouter, on a real
Cloudflare posting, both sides answering with zero provider failures — reported
ollama 11 rewrites against 25 guard refusals, and openrouter 12 against 1. Read
straight, openrouter won decisively. The content said otherwise, and three
defects behind that reading are now fixed.

**A skills line is a list however long it runs.** `classify` consulted
`_is_list` only *after* the length fallback, so `Languages  Python, TypeScript,
Rust, C, C++, Java, ...` was nineteen words and therefore "prose". It went to
the model, which returned `Using tools, I have experience with Python and
GitHub Actions.` — a list rewritten as a sentence, naming fewer things than it
started with. Two of them also carry a category label, which hid them from the
plain comma test as well: splitting `Data, DevOps & Tools  Qdrant (Vector DB),
Docker, ...` on commas puts the label and the first entry in one long fragment.
`_is_labelled_list` splits at the column gap first.

Moving the list test ahead of the length test is only safe because the verb
test already ran, and because `_is_list` now requires no terminal full stop —
otherwise a real bullet built from short clauses would be filed as a stack and
silently skipped, which CLAUDE.md already records as the harder failure to
notice. On the owner's résumé this took the lines sent to the model from 37 to
16, and every one of the 16 is prose.

**A rewrite nobody can see was being counted as a rewrite.** `changed` was
`candidate.strip() != bullet.strip()`, so a colon added to `Core CS  Data
Structures` and two spaces removed from `Cloud Data Warehousing & BI Analytics
[GitHub]` both counted. Three of openrouter's reported 12 were of that kind;
nine were real. `is_substantive` compares the lines with punctuation dropped and
spacing collapsed, and a cosmetic answer now keeps the source line so the
document and the count agree. Case is deliberately *not* folded — a model
lowercasing a technology name has changed something worth showing.

**Deleting a true thing is not fabrication, and nothing was checking for it.**
Every test in `guard.py` reads the output and asks whether it says something the
source does not; none reads what is missing. Both models dropped
`(Pillow/Tkinter)` from the same bullet and neither was refused, so the résumé
came out of tailoring with fewer of the keywords an ATS scans for.
`packages/tailor/technologies.py` reads the technologies the résumé lists for
itself — the skills section and the stack line under each project — and `vet`
refuses a rewrite that drops one.

Scoped to *listed* technologies rather than to names, because
`extract_entities` reads `Filtered`, `Provisioned` and `Designing` as proper
nouns and rejecting every dropped one would refuse the re-emphasis §2.1 permits.
Presence is decided through the alias table, so expanding `CI` to `continuous
integration` keeps the term — that is a rename, not a deletion. A flat corpus
has no inventory and disables the check rather than guessing one.

**What none of this fixes.** Both models still write worse bullets than the
source in places — openrouter inserted `using forecasting and clustering tools`
into a Tableau line and turned `vector retrieval pipeline` into `vector
retrieval data pipeline`. The refusal count measures how hard a model pushed
against the guard, not whether the résumé got better, and tuning against it is
the trap `docs/REFERENCE.md` §3.6 names. These three changes make the counts
mean what the screen says they mean; they do not make either model good.

## A rewrite that dropped a number

Added 2026-10-13, after the notes above were moved here. The owner asked for
one résumé tailored for one posting with the scores before and after. Eight
rewrites were accepted, no keyword was gained, the recruiter score fell from
57% to 51%, and two of the eight had lost figures: a model's test score and
the span of its data from one bullet, a count from another. *Deleting a true
thing is not fabrication* had been closed for technologies (above) and not
for numbers.

`packages/tailor/figures.py` reads the figures a bullet states, and
`rewrite.vet` refuses a rewrite that leaves one out or alters it. What a
later change could quietly undo (`tests/test_figures_not_dropped.py`):

- **It runs last.** Every rewrite refused before it existed is refused for
  the reason it always was; this only refuses what used to be accepted. A
  rewrite that drops a technology and a number is still "drops 'pytest'".
- **A value, not a spelling.** `1,000`, `1000` and `1k` are one figure, and
  so are `30%` and `30 percent`, `12 million` and `12M`. `30%` and `30` are
  two: the fabrication guard strips the sign and reads both as 30, so this is
  the only check that sees that change.
- **A year is not a figure.** The guard reads a bare 19xx or 20xx as a year,
  and this reads it the same way unless the text marks a count (`2,000`,
  `2000+`, `over 2000`, `2000 requests per second`). A bare `2000 requests`
  reads as a year. Phone numbers, addresses, links and page numbers are
  masked; a digit inside a name (`EC2`, `GPT-4`) is the name's.
- **The number is on the result and not in the log.**
  `BulletRewrite.dropped_figures` and `rejected_reason` name it;
  `tailor_rejected` logs a count (§10).

Measured on the rewrites already stored in the owner's résumés, as the
second measure §7 asks for: of 16 rewrites three other models had written
and the old guard accepted, it refuses none; of the eight in the run that
prompted it, it refuses the two that lost figures.

Seen and left alone, because both fail towards the résumé's own bullet: the
recombination check refuses `1000 billing records` for `1,000 billing
records`, and the fabrication guard refuses `1k` as a number the source does
not have. Not covered: a unit written apart from its number (`30 ms` to
`30 seconds`), a number the source wrote as a word, and a résumé edit made
over MCP, where `revise.guard_edit` reads only the lines that were added.

## Revising only where a term can be gained, and reading the PDF back

Added 2026-10-13, at the owner's asking, from their fork of `ai-job-search`
(`.claude/commands/apply.md`, step 5d): sort a posting's keywords against
the CV four ways, then check them against the PDF's extracted text. A model
does the sorting there. Here it is computed.

Why it was worth building. Rewriting all sixteen bullets for one posting
gained no keyword, and could not have: `ats.score` counts a term the résumé
backs under any alias, and the guard refuses a term it does not back. Sorted
against that posting with no model called, 13 of its 36 terms are stated, 21
are gaps, and one bullet has anything to gain ("user interface" for the `UI`
it says).

`targeted.py`, `practices.py`, `verify.py`, `scripts/tailor_targeted.py`.
What a later change could quietly undo (`tests/test_targeted_tailoring.py`):

- **No opportunity, no call.** The provider is not built, and the report says
  "no supported keyword gain available".
- **Only the bullets with evidence are sent**, with their terms and the
  evidence, and not the posting.
- **`rewrite.vet` is unchanged and still decides.** On top of it a targeted
  revision may take no word out of the bullet (`taken_away`), which is what
  holds units, model names and dates, and every posting term the bullet was
  not sent for is off limits to it. So a term goes only where its evidence
  is, and a side project's tool does not become an employer's claim.
- **One repair, then the bullet is the résumé's own.**
- **Nothing is attached, cached or selected.** The script publishes a version
  with no key and touches no match and no application.
- **Like is compared with like.** A tailored résumé's `raw_lines` are still
  the source's (`apply_rewrites`), so `verify.read` scores a copy whose text
  is its sections; and the PDF comparison renders the base with the same
  template instead of reading the owner's uploaded file.

**`practices.py` is a §2.1 decision.** `aliases.py` refuses a tool and its
practice as an alias, and that stands. This is one way, in the bullet that
names the tool, with the tool still in the sentence, and only in the targeted
path: the ordinary rewrite still refuses "automated testing". One row, which
the owner asked for by name. A second row is a decision of the same kind.

Found on the way:

- **`borrowed_terms` missed a term that ended a sentence.** The guard's token
  pattern keeps the full stop, so "…in Python for payments." did not borrow
  "payments". Fixed, in the ordinary path too (`tests/test_keywords.py`).
- **The fabrication guard reads "automated" and "testing" as ordinary
  English.** What refuses the phrase in the ordinary rewrite is the off-limits
  list, and only when the posting's top terms include it.
- **#157 and #158 failed together.** A test that came with #157 held that the
  guard accepts a cut-off bullet that lost its number; #158 refuses it. #158's
  checks had run before #157 merged. The test now uses a fragment that kept
  its number.

Not done, and worth knowing:

- **The scan score falls when a term is gained below the top of the page.**
  It is a share of matched terms, so the report flags it. Nothing at the top
  changed.
- **The term list is noisy.** It is `ats.score`'s own, and for that posting
  it holds "how", "internal" and the company's name. They are gaps and stay
  gaps; one ("how") is "supported" by a GitHub project whose description uses
  the word.
- **A synonym in the skills list only** is reported and not revised: a skills
  line is a list and no model is asked to rewrite one.
- **The ordinary pipeline was not changed here.** Apply and the nightly batch
  rewrote every bullet when this was written. Switching them is a decision
  about what gets attached to an application;
  `docs/TARGETED_TAILORING_INTEGRATION.md` says where that stands.

## The same day's corrections to the targeted revision

Added 2026-10-13, after #159 merged, at the owner's asking.

**pytest evidence is a statement, not a name.** `practices.py` shipped with
thirteen test runners and read the practice into any bullet that named one.
The row is pytest now. `affirmed_in` wants the bullet to say the tool was
used for tests or checks, with nothing near denying it and nothing before it
making it a plan; a list item is not a statement however the line opens
(`Testing: pytest, Playwright` is a bullet to `bullets.classify`). The skills
list is no longer a source. `tests/test_practice_evidence.py`.

**A kept revision keeps the claim's scope.** `brought_in` refuses any word
that is neither the bullet's nor a target's ("across the company" is ordinary
English to the guard), and `stands_beside` wants the practice next to its
tool and not after an "and" or a "not". A third check, that the revision
still affirms the tool's use, was written and removed: nothing could reach it.

**Grammar is no term** (`ats._GRAMMAR`). "how" was one of the posting's 36
"requirements". Over 300 open postings 190 scored terms were closed-class
words. On the measurement side only: `keywords._STOPWORDS` is the guard's.

**Half a word is no support** (`keywords._stands_in`). `_supported` looked
for a term anywhere in the résumé's text, so "user experience" was backed by
the `ux` in "Linux", "RESTful APIs" by "Random Forest" and "code" by
"cross-encoder". This was the guard's input and not only a score: supported
terms are offered to the model as safe and left off the off-limits list. The
full rewrite published as v11 wrote "user experience" into a bullet about a
command-line tool for that reason. v11 is attached to nothing.
`tests/test_keyword_noise.py`.

For that posting the scorer's list went from 36 terms to 35, and the base
résumé backs 12 of them where it had been credited with 15.

Left as found: `C#` is two characters and was never a term; the scorer does
not count "engineers" as backed by "engineer" and the targeted classifier
does; the employer's own name is still a term.

The plan for putting the targeted revision behind apply and the nightly
batch is `docs/TARGETED_TAILORING_INTEGRATION.md`, which also says how much
of it is built.

## The face, and fitting a résumé to its pages

Added 2026-10-18, at the owner's asking, after a look at what Tsenta's app
offers: five faces, a "One page" toggle and justified text. `style.py`;
`tests/test_resume_style.py`.

Two defects it answers. The owner's `v4/tailored.pdf` ran to three pages with
7 lines on the third, and `layout.py` could only report it. And the template
asked for "DejaVu Sans", which this Mac does not have, so every PDF printed in
Helvetica while the stylesheet said otherwise.

What a later change could quietly undo:

- **The default style is the template as it was, character for character.**
  `RESUME_CSS` is `resume_css(ResumeStyle())`, and the test compares it with
  the old literal. Every page-layout fixture counts lines on that document.
  `AssemblyOptions.fit` is off for the same reason: a bare `assemble_pdf` is
  the template. Tailoring, an edit at review and the cover letter take the
  face and fitting from settings (`publish.house_options`).
- **Fitting changes spacing and size, never a word.** Steps in `FIT_STEPS`,
  spacing before type, down to `FIT_FLOOR`: 9.5pt body, 1.2 line spacing,
  0.5in margins. A target the floor cannot reach keeps the ordinary type and
  says so: squeezing to the floor and still running over costs the reader
  and buys nothing. Cutting stays the owner's call, on the review screen.
- **A spill is up to a quarter of a page** (`SPILL_SHARE`), wider than the
  6 lines `layout.py` reports as thin. The `v4` résumé's 7 lines were one
  over that, and one step pulled them back to two pages at 10pt.
- **The gap under a bullet is part of the style.** Fixed at 3pt while
  everything else tightened, it held a bullet-heavy page to 14% more lines
  at the floor, short of a 7-line spill on page 2.
- **Only faces macOS installs are offered.** fontconfig answers Calibri with
  Verdana and Garamond with Times New Roman.
- **A face is checked in the PDF, by letters.** WeasyPrint names an embedded
  font after its family, so Times New Roman arrives as
  `Times-New-Roman,-Bold`. A prefix check written against Georgia passed and
  read Times as not installed on the owner's résumé. The test asks every
  face fontconfig has, with fontconfig as the judge, because asking
  `face_available` would skip exactly when the check is wrong. A face that is
  not embedded falls back to the default face and the note says which one
  was asked for.
- **How a stored résumé was set is recorded** beside `MODE_STAMP`, as
  `STYLE_STAMP`: the face, the sizes and the note. Not yet shown on the
  review screen.

Not taken from Tsenta: justified text (uneven word spacing for no parsing
gain) and its "Aggressive tailoring … may stretch the truth" mode, which is
§2.1's line.
