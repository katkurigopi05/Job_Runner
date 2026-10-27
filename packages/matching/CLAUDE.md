# Notes on matching, the feed and the search area

Moved here from the root `CLAUDE.md` on 2026-10-11, word for word, so they
load only when work touches this directory. "§N" below is a section of
the root file, which keeps every heading and says where each section went.

- The assistant's search (`retrieve.py`, `rerank.py`, `roles.py`, `gaps.py`, `evidence.py`, `chunks.py`) is in `.claude/rules/assistant-search.md`, which loads with those files.
- Hybrid retrieval, `hybrid.py` (§19), is in `docs/CLAUDE.md`.

## From §15: What the gates do and do not prove

### Remoteness was allowed to override the region

The owner's search area is one sentence — **California on-site or remote, the
rest of the United States remote only, nothing abroad** — and until now no
single place in the code held it.

`filters.location_matches` was the hard filter that decides whether a `Match`
row is written at all, and it began `if is_remote(posting): return True`. So
the word "remote" anywhere in a location bought a posting past the region
check entirely. Against the twelve crawled postings the one survivor for a US
profile was **`Canada - Remote (ON, AB, BC, or NS Only)`**; `Remote (India
only)` and `Remote - EMEA` pass the same way. A remote job the owner is not
eligible to hold is still a job they cannot hold, so remoteness is the wrong
thing to short-circuit on.

The other half of the filter compared the posting against `profile.location`
as a substring, which §1 rules out directly: a search area is the owner's
input, not a reading of their profile. It also answered the wrong question —
"is this near where I live" rather than "did I ask to see this" — and made
moving house silently rewrite the feed.

`locality.reachable` was meant to be the single statement of the area, with
the scoring gate (`filters.location_ok`) and the feed (`search.matches`) both
calling it. ~~Neither did~~ — each kept its own copy and `reachable` had no
caller. Corrected 2026-10-02: see *Three rules for one area* below.
`Settings.search_remote_outside_california` supplies the standing preference
and `?remote_outside_california=false` turns it off for one call, which is
what relocating would want. It is a separate setting from `search_us_only`
because it is separately reversible.

Three findings came out of pointing it at real data rather than fixtures:

- **A bare "Remote" was classified as probably-foreign.** `locality_of` had no
  case for a location that names a working mode and no place, so "Remote" fell
  through to `UNPLACED` — the class the corpus says is foreign — and `us_only`
  dropped it. That is the commonest way a *domestic* board writes exactly the
  job the owner is looking for. A mode-only string is now `UNKNOWN`, which is
  what it means: no evidence about where. `UNPLACED` **is now kept too** —
  see the merge note at the end of this section.
- **"distributed systems" meant remote work.** `distributed` was in the remote
  vocabulary and that vocabulary was matched against the *description*, so any
  posting mentioning distributed systems read as offering remote work — and
  under the area rule that converts an on-site role in any state into a
  reachable one. On the twelve crawled postings the old vocabulary called
  **7 of 12 remote and the new one calls 4**; all three that flipped mention
  "distributed", and one of them — `Sr. Manager, Field Engineering`,
  `Northeast - United States` — was kept by the area filter purely on that
  misreading. A location field is a declaration about the job and prose is
  not, so the two now have separate vocabularies: the body needs "distributed
  team", "work from anywhere", or "remote" proper.
- **There were two definitions of remote.** The feed read the description with
  an on-site override; the scoring gate read only title and location. Under
  this rule that disagreement is the difference between a kept job and a
  dropped one, so `locality.reads_as_remote` is now the only definition and
  both call it.

On the twelve crawled postings the area keeps **none of them**, and that is the
right answer rather than a regression: the board is a Palantir sweep with no
Californian and no US-remote roles on it — every posting is abroad or on-site
in another state. Worth stating plainly, because "0 kept" and "the filter is
broken" look identical from the summary line.

Two things this did **not** fix, filed rather than papered over:

- ~~`rescore` leaves a `Match` row behind when a posting newly fails a hard
  filter, so its own "top 1" still prints the Canadian role it just
  excluded.~~ Fixed: `score_and_store` withdraws the stale row and `rescore`
  reports the count, so the live run now reads "0 kept (12 excluded by a hard
  filter, 1 withdrawn)" above "top 0" rather than above the role it excluded.

  Withdrawn **only when the row holds nothing of the owner's**. `decision` is
  their swipe and `tailored_resume_id` is a résumé some provider call already
  paid for; deleting either to tidy up a score would throw away the more
  valuable half of the row to fix a cosmetic one. Those rows stay, and the
  read-time filter goes on hiding them —
  `test_a_decided_match_is_kept_even_when_it_stops_qualifying` holds that line.
- ~~The US city and non-US city lists in `locality.py` are hand-written, so a
  posting in a US city on neither list reads as `UNPLACED` and is dropped.~~
  Measured and closed twice over — the lists were extended, and then
  `UNPLACED` stopped being dropped at all, which removes the failure mode
  rather than narrowing it. The lists are still hand-written and still matter
  for *precision*: an unplaced Californian city is kept, but as a maybe rather
  than as California.

### Merging with the country fix that landed on main

Two sessions fixed the same filter from different ends, and the merge is worth
recording because each caught what the other could not see.

**main had the better data.** It ran against a real crawl of all 119 companies
and found that `location_matches` was a substring test: the owner's profile
reads `san fransico , ca,usa`, and `ca` is inside `canada`, `costa rica` and
`vancouver` and inside none of `united states`. Every Canadian role passed as
Californian while American ones were dropped — the top of the feed was four
Elastic roles in Canada and a finance manager in Costa Rica. It also found
`Spain (Remote)`, `United Kingdom (Remote)` and `Republic of Ireland (Remote)`
sitting in the top three, and two Synthesia roles located simply `Europe`. Its
`locality.py` gained thirty-odd countries plus continents and blocs, and
deliberately left out `georgia`, `panama`, `lebanon` and `jordan` because
those names are American places and rule 1 runs before any state *name* is
read.

This branch had only the twelve-posting Palantir sweep, so none of that was
visible here.

**Three things were reconciled rather than picked:**

- **`UNPLACED` is kept.** main's argument — only an explicit foreign signal
  should exclude, a city no rule recognizes should be ranked down rather than
  hidden — is right, and its own country work is what makes it right: most of
  what used to land in `UNPLACED` now lands in `ELSEWHERE`. It also dissolves
  the 143-missing-cities problem instead of managing it.
- **The region rule stays, because the owner asked for it.** main read *which
  part* of the US as a ranking question, which is correct in general and is
  what `locality.rank` already does. The owner overrode it: on-site outside
  California is a move, not a commute. `remote_outside_california=False`
  restores main's reading exactly, and the test that used to assert it now
  asserts both halves.
- **A bare `United States` is not "outside California".** main's test caught
  this: `UNITED_STATES` covers both `Austin, TX` and a bare `United States`,
  and the region rule was dropping the second. `locality.names_us_region`
  separates them — a location that names no region is no more evidence than
  silence.

### The city lists failed closed, and California was the worst of it

Filed as a footnote to the search area and measured immediately after, because
"a domestic city that is missing fails closed" turned out to describe most of
California. Of 205 Californian cities, **143 did not classify** as Californian
from a bare name — Santa Ana, Stockton, Chula Vista, Palm Springs, Beverly
Hills among them. Under the area rule that is precisely an on-site Californian
job, the one category the owner most wants, dropped in silence.

Three distinct failures, which is why counting them separately mattered:

- **Simply absent** — `Santa Ana` → `UNPLACED` → dropped.
- **Claimed by a substring of another US city** — `Manhattan Beach` matched
  `manhattan` and `La Mesa` matched `mesa`, so both read as `UNITED_STATES`.
  Right country, wrong state, and on-site outside California is dropped.
- **Claimed by a foreign city** — `Dublin` → `ELSEWHERE`.

The first two are now fixed: 135 Californian and 37 other US city names added,
and 177 of 178 unambiguous Californian cities place correctly (Menifee was the
one that got away in the first pass and is in too).

The third is deliberately *not* fixed. `Dublin`, `Ontario`, `Orange`,
`Fairfield`, `Norwalk`, `Westminster`, `Brentwood`, `Carson`, `Davis`,
`Martinez` and a dozen more are real Californian cities whose bare name reads
as somewhere else at least as often. Adding them trades a missed job for a
false positive, and here the false positive is worse: it puts a Dublin,
Ireland role in a feed whose on-site tier is supposed to mean California. Every
one of them classifies correctly the moment a board writes ", CA", which is how
the smaller ones are almost always written. `_AMBIGUOUS_CALIFORNIA_CITIES`
records the decision so the next reader knows it was one.

Two things worth keeping visible:

- **The non-California half matters only because of remote.** An on-site job
  in Bentonville is dropped by the area rule regardless. But `UNPLACED` is
  dropped *even when remote*, so a missing US city name costs exactly the
  remote roles the owner would take. 42 of 88 checked names were missing.
- **A de-duplication pass silently moved `richmond` and `santa rosa` out of
  California.** A name in both a Californian list and the US list is
  unreachable in the second, since California is checked first — so removing
  "the duplicate" removed the live one and both cities became merely American.
  `test_no_city_is_claimed_by_two_lists` now fails on that, which is how it
  was caught.

This does not make the approach right. Hand-written lists are still the reason
a city can be missing at all, and the honest fix is a real place-name dataset
rather than a longer tuple. What changed is the size of the hole and whether
a regression in it is visible.

### Three rules for one area, and a list read as one place

Found 2026-10-02 by running every open posting's location through the rules
then live: 5,948 postings, 1,133 distinct strings. The paragraph above said
`locality.reachable` was the single statement of the area. It had no caller.
The scoring gate and the feed each kept a copy, and the chat search had none
(§14), so three surfaces read the area three ways.

**The copies disagreed on 107 postings.** The gate kept a place no rule
recognized (`UNPLACED`), as this file says it should; the feed dropped it, as
its own field note said. Thirteen of the 107 were "SF Office".

**A list was read as one place.** `locality_of` classifies the whole field and
its first decisive rule wins, so one `, ON` condemned "San Francisco, CA;
Pittsburgh, PA; Toronto, ON; Dallas, TX" as foreign.

**And the vocabularies had gaps** that only the real corpus shows:

- accents: "Zürich" and "Malmö" never met `zurich` and `malmo`;
- `\b` after a full stop needs a word character next, so "U.S. Remote" never
  matched `_US_COUNTRY_RE`, and nor did "Remote-United-States";
- a state code with no comma ("Remote - OR", "UT - Cottonwood Heights");
- a foreign city beside its own country's code where that code is also a
  state's: "Toronto, CA" was California and "Berlin, DE" was Delaware;
- provinces by name, and placeholders like "N/A" read as places.

The fix, in `locality.py`:

- **`area_exclusion` is the only rule**, built on `reachable`, and the gate,
  the feed and the chat search all call it.
- **Each listed office is judged on its own** (`locations_in` splits on `;`,
  `|`, and "or", but not `, OR`, which is Oregon). An office in the area keeps
  the posting; otherwise an explicit signal (on-site in another state, or
  abroad) excludes it; otherwise it is kept. A remote option listed beside
  American offices is remote within the country ("Houston, TX or Remote").
- **The vocabulary gaps are closed**, each with a real string as its test, and
  the strings that must *not* move are tested beside them: "Dublin, CA",
  "Paris, TX", "Vancouver, WA", "Portland, OR".

Measured after: 0 disagreements, and `UNPLACED` from 107 postings to 22
(regions that include the US, such as "North America", and three stragglers).
The feed and the chat keep 92 more postings, American jobs they had been
dropping, and drop 3 foreign ones they had kept. The scoring gate, which had
kept every unrecognized place, keeps 20 more and drops 38: Malmö, Zürich,
Frankfurt, "Toronto, CA" and the like, plus on-site Wichita and Chicago.

**What keeps it right is `make audit-locations`**, not the vocabularies. They
are hand-written and will have gaps again; a gap does not raise, it keeps a
foreign job or drops a local one and the feed looks the same. The command is
read-only and prints the unrecognized places and the strings most likely to be
misread. Run it after a crawl that adds boards, and add what it finds to
`locality.py` with its string in `tests/test_location_area.py`.

### The labeling loop, and the bias it had to avoid twice

`docs/BACKLOG.md` P1 — "the one that matters most", and the blocker under
every ML claim in this repo. Every relevance label here is a `FIXTURE`: a
posting and a grade written together, beside the code that reads them. That is
why `docs/ML_EVALUATION.md` refuses to name a production ranking candidate,
and why §15 above says Gate 5 does not answer the question it was written to
ask.

`/label` is the screen that changes it. A real crawled posting, a 0–3 grade,
exported as `Provenance.OWNER` by `make export-labels kind=owner`.

**The design decision worth recording is what it is *not*.** The obvious build
is `/swipe` with four buttons instead of two. `packages/matching/feedback.py`
already names two weaknesses in swipe-derived labels, and that build fixes
exactly one of them:

- a swipe is **binary**, so it cannot tell "would apply" from "would drop
  everything for" — which is precisely what NDCG's `2**rel` gain exists to
  reward;
- a swipe is **taken in feed order**, so it is only ever recorded for postings
  the ranker already surfaced. Nothing it buried is ever labeled, and the model
  ends up graded on its own shortlist.

A four-point scale on the same feed fixes the first and leaves the second
untouched — while stamping the result `owner`, the provenance a benchmark
trusts *most*. That is worse than not fixing it at all, because the bias stops
being visible. A `FEEDBACK` label announces its own narrowness; an `OWNER`
label carrying the same narrowness does not.

So `packages/matching/active.py` draws from three streams:

- **uncertain** — scored near the middle of the *observed* range. Read rather
  than assumed: §15 records that the shipped `min_match_score` of 0.75 was
  unreachable when the first real run over 10,922 postings peaked at 0.271, so
  a midpoint hardcoded at 0.5 would call nothing uncertain and this stream
  would silently return empty.
- **unseen** — crawled but never scored for this profile, including postings a
  hard filter dropped. These have no `Match` row at all, so they can never be
  swiped, and before this they could never be labeled either. They are the only
  labels that can measure what the ranker is missing.
- **confident** — its own top picks. If those grade 0, the problem is not the
  threshold and no amount of boundary sampling will show it.

Four things are load-bearing:

- **`PostingLabel` is its own table, not a column on `Match`.** A `Match` row
  exists only for a posting that cleared the hard filters and got scored.
  Keying on (profile, posting) instead is what makes the unseen stream
  storable at all.
- **The stream is recorded per label.** It is the audit trail for the bias:
  a corpus that turns out to be all `uncertain` has the shortlist problem
  back, and after the fact that is otherwise unknowable. `/labels/summary`
  says so outright when no unseen grade exists yet.
- **A scored posting can never be recorded as `unseen`.** Found by a test:
  the first classifier fell through to `unseen` for any score far from the
  midpoint, so the summary would have reported a shortlist-only corpus as
  having escaped the shortlist — the one wrong answer that column exists to
  prevent. `unseen` now means one thing only: the ranker never scored this.
- **A short stream does not backfill from the others.** On a database with
  nothing unscored the unseen quota goes unfilled rather than being handed to
  `uncertain`. Backfilling would quietly return an all-shortlist batch, which
  is this bias arriving through the back door.

**What is not done, and no code can do it.** The corpus is empty. Grading is a
person reading real postings. P1's "done when" is now ≥100 labels *across more
than one stream* — the count alone was never the property that mattered.

### Every path read one résumé, and two of three were unreachable

`docs/BACKLOG.md` P2, and the smallest real defect in the register. Every path
read `profile.base_resume_id`: upload a backend résumé, a data one and an ML
one, and the application always starts from whichever the profile happens to
point at.

Silent, which is what makes it worth recording rather than just fixing.
Nothing raises, nothing is logged, and the tailorer does its whole job — the
guard runs, the projects are selected, the diff renders. It rewrites the wrong
document well, and the employer receives a competent ML résumé for a backend
role. The only way to notice is to remember which résumé the profile points
at.

`packages/matching/pick_resume.py` scores each of the owner's base résumés
against the posting and takes the closest. Four things are load-bearing:

- **Tailored rows are excluded.** They live in the same table, and a résumé
  already bent toward another job is the one selection that would be actively
  harmful. `tailored_for_posting_id` is the discriminator — `publish.py` sets
  it on every tailored row including the uncacheable ones where
  `tailored_key` is NULL, `revise.py` carries it across an owner's edit, and
  it is a foreign key, so a tailored résumé always names a posting that
  exists and can never read as an upload by accident.
- **A win inside the noise is not a win.** Two résumés for adjacent roles
  score within thousandths of each other on most postings, and letting that
  decide would change the document an employer receives between two runs of
  the same posting. Below `MIN_MARGIN` the profile's own `base_resume_id`
  wins — the owner's standing choice is the tie-break, not the float.
- **The choice is recorded and then reused.** Approving a parked application
  re-enters the pipeline from the top, so re-deciding would let an upload
  between the two runs swap the document the owner reviewed. Same defect as
  `resume_pinned`, one screen earlier.
- **It takes the posting's text, not a `Posting`.** The apply pipeline hands
  `_tailor` a parsed page rather than a stored row — the parameter is `Any`
  for that reason — so depending on the model would have failed on the one
  caller that matters. Two existing tests caught it.

`/review` names the résumé it started from, its reason, and the runners-up
with their scores. A selection with no alternatives shown is unauditable, and
this decides which document an employer reads first.

The cover letter is written from the same document. A letter drafted off a
different résumé than the one attached would contradict it.

### One regex was answering three questions about work authorization

Also reported from the source, also confirmed by running it. `filters.py` had a
single pattern list for sponsorship, and it was wrong in both directions at
once:

```text
"We do not offer visa sponsorship."                    kept      (wrong)
"We cannot sponsor now or in the future."              kept      (wrong)
"Candidates with or without sponsorship ... apply."    EXCLUDED  (wrong)
"US citizens only."                                    kept, unflagged
```

The first two are false negatives, and they are the *commonest* English form of
a refusal: the list matched literal phrases like `unable to sponsor` and never
learned negation, so `do not offer` sailed through. The third is the expensive
error — `without sponsorship` is a substring of `with or without sponsorship`,
a phrase that exists to say sponsorship is **not** a barrier, so the postings
that go furthest out of their way to welcome the owner were the ones excluded.

`packages/matching/eligibility.py` reads the posting instead of scanning it,
and four rules shape it:

- **Silence is not an answer.** A posting that never mentions sponsorship has
  said nothing — not "available", not "none". §2.2's caution about work
  authorization applies to reading as much as to answering.
- **Ambiguity is not an answer either.** `with or without sponsorship`, and a
  posting that both refuses and offers, are `AMBIGUOUS`. Neither excludes: a
  hard filter needs evidence, and a contradiction is not evidence.
- **Citizenship is a separate fact.** "US citizens only" is not a statement
  about sponsorship. A permanent resident needs no sponsorship and still fails
  it, and no employer generosity fixes it. It gets its own verdict and its own
  exclusion reason.
- **Nothing is inferred from an absence.** No OPT or STEM-OPT acceptance, no
  E-Verify participation, no history of sponsoring. A feed that labelled a job
  "OPT-friendly" because the posting happened not to mention visas would be
  inventing the one fact an applicant cannot afford to be wrong about.

`profiles.citizenship_status` is the profile half, and it is deliberately
**not** `work_auth`. §2.2 keeps that field verbatim for forms; this one is a
search filter (§1: the owner's input, not a reading of their profile) and is
never typed onto an application. NULL is what every existing row gets and means
unstated — the filter then *surfaces* an explicit restriction rather than
excluding on it, because dropping a posting on a field nobody filled in hides
real jobs, and parsing a legal status out of free text to decide which jobs the
owner ever sees would be the §1 violation in the other direction.

The feed shows the verdict with the posting's own sentence, every time,
including when the answer is "Unknown — verify with employer". A card that
carried an authorization line only when a restriction was found would teach the
reader that a missing line means "fine".

One incidental find, from a test rather than from reading: `Must be a U.S.
Citizen or Green Card holder.` split at the full stop in `U.S.`, so the
citizens-and-residents rule — which needs both halves in one clause — never
fired and the posting read as citizens-only. A permanent resident would have
been excluded from a job that names them.

### A posting could demand ten years and nothing would notice

`docs/BACKLOG.md` P7, and the half of it that is buildable offline. `Posting`
records no experience requirement at all, so a role asking for ten years
reached the feed ranked by cosine similarity alone — and §15 already records
what a cosine cannot do here: `filters.seniority_ok` exists because a Junior
Backend Engineer with an excellent technology match ranks in the top ten on
title and body similarity, and no amount of scoring refuses a rung.

`packages/matching/experience.py` reads the demand. **The split is the point,
not the number**: a years line under "Minimum qualifications" and the same line
under "Things we love" are different facts, and only a `MANDATORY` one is
allowed to exclude. `filters.experience_ok` checks it against
`profiles.max_required_experience_years`, and `rubric._experience` is where a
preferred demand the owner is under lowers a dimension rather than hiding a job.

Measured on the twelve real crawled postings in `tests/fixtures/golden/`, which
is where the rules came from rather than the other way round:

| | |
|---|---|
| postings stating a years requirement | 7 of 12 |
| demands found | 9, **all 9 classified** — none left ambiguous |
| ranges | read at the lower bound: `8–12+ years` is eight, `10-15 years` is ten |
| prose mentions of a year | "A Year at Palantir", "Come join us for a year" — neither is a demand |

Four decisions carry weight:

- **The smallest mandatory demand binds, not the largest.** One posting lists
  "8-10 years of experience in Strategic Finance" and "2+ years of investment
  banking"; this module cannot tell a conjunction from an alternative, so it
  keeps the posting at 2 and shows both. Hiding a job the owner might hold is
  invisible, which is the same asymmetry `locality.py` settled for an unplaced
  city.
- **Neither a connector nor an experience noun is sufficient.** `3+ years field
  experience` never says "years of" and `2+ years of investment banking` never
  says "experience", so a line counts when an experience noun follows nearby
  **or** the line opens with its years phrase — which is how a qualification
  bullet is written and is not how prose mentions a duration ("able to commit
  to 2 years in the role"). Lines are read one at a time so a duration cannot
  borrow the next bullet's noun.
- **An unrecognised heading clears the previous one.** Without that reset a
  years line under "Benefits" or "Pay Range Transparency" — both real headings
  from this corpus — inherits `MANDATORY` from a requirements list several
  screens above, and the filter excludes on a perk.
- **Three of the seven needed heading wording taken from the corpus.** `What We
  Look For`, `What you bring to the table` and `Your background looks something
  like this` were all unrecognised at first and read as `AMBIGUOUS`, which never
  excludes — so the filter would have looked armed and done nothing. Palantir's
  pairing is the one worth remembering: `What We Value` is the nice-to-have
  list and `What We Require` is the requirement, and the first reads like a
  statement of principles.

`tailor/ats.py::_REQUIREMENT_HEADING` is deliberately not reused. It answers
where the requirements *start* — it matches "What You'll Do" on purpose — and
cannot say whether a heading is asking or wishing, which is the only thing this
module wants from one.

**Nothing here touches the scorer.** `Match.score` stays the cosine for the
reason `rubric.py` gives at length, so the preferred half costs a visible
dimension and not a place in the feed. Tuning the ranking function here would
invalidate Gate 5 and bury the reason in a float.

#### Two filters the owner could not set

Found while wiring this one, and it is the defect §15 already records for
`citizenship_status` — a column, a schema, a filter and tests, with no control
anywhere, so the filter could never fire on real data.

**`target_seniority` had shipped exactly that way.** It has a measured payoff —
P@10 from 0.900 to 1.000 on the Gate 5 set — and the only way to set it was
curl or the MCP tools. `/profile` now has both controls, in a fieldset of their
own headed "Which postings you see", because §1's distinction deserves to be
visible on the screen: these narrow the feed and are never typed onto an
application.

Two details in the form are load-bearing:

- **The bound cannot go through the form's empty-string mapping.** `Number("")`
  is 0, and 0 is a real answer here — "only roles that ask for no experience"
  is what a new graduate wants — so a blank box mapped through it would become
  the strictest filter in the app rather than no filter at all.
- **The CHECK constraint is declared on the model as well as in the
  migration**, because `alembic check` is a gate and a constraint the models do
  not know about reads as schema drift.

`tests/test_experience_requirements.py` is in `GATE5_TESTS`. Its positives are
verbatim from the crawled corpus; what the corpus cannot prove is stated in the
module docstring rather than implied — **no posting in it puts a years line
under a nice-to-have heading**, so the PREFERRED path is exercised with real
heading wording around a constructed placement, and the filter's refusal to
exclude on it is argued rather than observed in the wild.

## §18: the feed could not be asked the one question that decides eligibility

`eligibility.py` has read sponsorship and citizenship out of a posting since it
replaced the single regex that was answering three questions (§15). The verdict
reached the match card and stopped there. `FILTER_KEYS` had 22 entries covering
location, seniority, pay, skills, education and freshness, and **none for work
authorization** — so the owner could read "states it does not sponsor" one card
at a time across a feed drawn from 14,892 postings, and could not ask the feed
for it.

This is the same defect §15 records twice for `citizenship_status` and
`target_seniority` — a column, a schema, a filter and tests with no control, so
it could never fire on real data — arriving through the other door. Here the
reading fires on every card and there was nothing to act on it with.

`sponsorship=available` and `exclude_citizenship_restricted` are the filters.
Five things are load-bearing:

- **An unknown is never a pass** (§16). `UNSTATED` is a posting that said
  nothing and `AMBIGUOUS` is one that said something unresolvable. Neither is
  an offer, and treating either as one would invent the fact `eligibility.py`
  exists to refuse to invent — one layer above the module that refuses it.
- **`include_unknown_sponsorship` is how the question is usually asked.** An
  explicit offer is rare, so `available` alone hides most of the corpus. With
  the switch the filter drops only a stated refusal, which is what someone who
  needs sponsorship actually wants day to day. Both halves are one key plus
  one switch rather than two vocabularies, because that is the idiom every
  other unknown-averse filter in `search.py` already uses.
- **Citizenship gets its own switch, not a sponsorship value.** A permanent
  resident needs no sponsorship and still fails "US citizens only", and no
  employer generosity fixes it. Collapsing them would rebuild the one-regex
  problem at the filter layer. It has no `include_unknown_*` twin on purpose:
  it excludes on a restriction the posting stated outright, so silence already
  keeps the posting and there is no unknown for a switch to widen.
- **The verdict is read only when asked for.** `authorization_reasons` returns
  before touching the posting when neither filter is set, because reading it
  scans the whole description and the dashboard's default request asks for
  neither — the cost the keyword filter is already guarded against, which
  §15 records falling from 2.5s to milliseconds.
- **It is a feed filter, not the scoring gate.** `filters.sponsorship_ok`
  already drops refusals when `Profile.needs_sponsorship` says the owner needs
  it, and that decides whether a `Match` row exists. This is the owner asking a
  question of the feed today. §1 keeps those separate and so does this.

`sponsorship` is a vocabulary rather than a boolean for the reason
`target_seniority` is a `SeniorityLevel`: a typo that reads as "no preference"
leaves the feed looking filtered with nothing to explain it. `sponsorship=yes`
is a 400 naming the accepted values.

**Three lists have to agree for a filter to work at all** — the bar writes a
key, `matches/page.tsx` forwards it, the API accepts it — and
`test_every_control_the_filter_bar_renders_reaches_the_api` holds all three.
Measured while adding this: no control was being dropped, so the test pins a
property that was true rather than fixing one that was broken. Five backend
keys (`us_only`, `remote_outside_california`, `allow_unknown_location`,
`allow_unknown_seniority`, `include_closed`) have no control and are reachable
only through a saved search or the API — deliberate, and the reason the test
asserts the bar's keys are a subset rather than asserting the lists are equal.

Where the idea came from is worth recording, because it was not a gap anyone
here had noticed. A friend of the owner's posted a four-agent LangGraph
architecture for a job assistant, and its worked example was *"Find Python jobs
in California that match my resume and explicitly offer sponsorship."* Three of
its four agents are things this repo does without a model, and its verification
agent answers a question `rubric.py` does not have — nothing here generates a
match explanation, so there is nothing to verify. What the post was worth was
the sentence: it asked the feed something the feed could not be asked.

**An assistant could not ask it either** (2026-10-07). The MCP tools reached
applications, résumés and the posting search, and nothing reached `/matches`,
so that sentence put to Claude Code had no tool to call. `my_matches` is the
feed over MCP. Three things about it:

- **It sends only what it was given.** Several of the feed's defaults are the
  owner's standing preferences (the search area is one), and a tool that sent
  its own default for each would overrule them without anyone having asked.
- **Its filters are held to the route's signature**
  (`test_every_filter_the_tool_takes_is_one_the_feed_reads`). FastAPI ignores
  a query parameter it does not know, so a filter spelt wrong in the tool
  would be sent, dropped, and read as no preference.
- **A match is cut to fifteen fields.** The feed's row carries the rubric, the
  legitimacy findings and the parsed requirements; ten of those would be most
  of a model's context. The tool's description also says the score is low by
  construction and not a percentage, which §15 found the dashboard getting
  wrong.

It reads and never writes: recording a decision on a match is still the
owner's, on `/matches` or `/swipe`.

## The keyword filter reads words, not letters (2026-10-20)

Asked for "software engineer ai", the feed's keyword filter, a substring test,
answered badly either way it was typed. As one keyword the role table read
"software engineer" and dropped "ai": 542 results, mostly not AI. As two, "ai"
matched inside "maintain" and "container". `keywords.py` replaced it. What a
later change could undo:

- **A keyword is whole words**, case aside, plural allowed: "ml" is not in
  "HTML", "go" is not in "Google", "c" is not in "C++".
- **Several words match as written; only a title also takes them near each
  other** (within `NEAR`, any order: "AI Software Engineer", "Software
  Engineer, AI Platform"). Nearness was tried in descriptions too, and over
  the live feed it matched one company's boilerplate ("open source software
  engineers and AI agents rely") on 20 unrelated postings, and words either
  side of a full stop.
- **A role name plus words finds the role's other titles only when the title
  says the words** ("Member of Technical Staff, AI Engineering", not "Member
  of Technical Staff - Inference").
- **A description is never split into words whole.** That cost 2-3.6s per
  search over the feed's 4,543 postings. A pattern finds where the keyword
  might stand and only a window around it is read; the pattern checks the
  character before a word *after* the word, which made it 3-30x faster with
  identical matches. A search costs 0.14-0.29s against 0.07-0.15s before.
- **The card's hits are read again for the page**, not carried out of the
  filter: grouping can put a different listing of the same job on the card.

Over the 4,543 open matched postings, before and after:

| Search | Before | After |
|---|---|---|
| `software engineer ai` | 542 | 50, every one an AI software role, all on the title |
| `software engineer, ai` | 927 | 538 |
| `ai` | 4,533 | 3,269 |
| `ml` | 1,574 | 497 |
| `go` | 4,090 | 1,344 |
| `react` | 276 | 197 |
| `software engineer` | 928 | 705 |
| `kafka`, `c++` | 81, 221 | unchanged |
| `machine learning` | 354 | 359 ("machine-learning" now counts) |

`software engineer` lost 229 postings that only ever said "software
engineering". That is the narrowing working, not a loss: the role filter is
the question "is this job a software engineer job".

## Postings with no description jammed the embedding batch (2026-10-26)

The owner asked why new postings were not all embedded. Of 19,768 open
postings, 8,382 had no vector: 7,846 Workday postings with no description
(the list endpoint returns none; the bounded detail pass fills them later)
and 536 older postings that had text.

`postings_needing_embedding` selected open postings with no vector, newest
first, 2,000 a pass. `embed_postings` skips a posting with no description, so
those never got a vector and were selected again on every pass: all 2,000
places, measured. Only postings newer than the jam got through, which is why
a crawl's new postings were embedded and the 536 never were. A Workday
posting whose description arrived later from the detail pass waited behind
the same jam.

The selection now requires a description, the rule `embed_postings` and
`chunks.chunk_postings` already used. On the live database the next pass
selects exactly the 536. `tests/test_incremental_matching.py`.

### Requirement evidence version 3

`required_skills.py` handles explicit, single-skill demand questions separately
from mention ranking. Its full candidate scan uses `skill_vocab` spellings,
including aliases, applies existing area/level/place rules, then classifies
requirements before selecting examples. An unread source prevents an exact
required total. Compound skill or role questions retain existing retrieval.

`requirements.py` records option status before truncating quotes, recognizes
Essential headings through the shared experience reader, and compares skill
clauses independently. Current stored readings are reused. Stale readings are
re-extracted in memory where source text exists; no live backfill is automatic.

### Place and remote intent

Recognized Bay Area names are consumed as regional constraints before choosing
search subjects. Their full original surfaces still prefer the named city;
sharing the word San does not make San Jose a San Francisco match.

An explicit remote work request filters every candidate path before selection
and full-set counts. Its percentage denominator remains all in-area postings.
Requirement and sponsorship paths retain the same mode constraint. The shared
locality reader rejects technical and negative remote mentions; negative
metadata overrides weaker company prose. Counts describe work-mode evidence,
not an independent verification of applicant eligibility.
