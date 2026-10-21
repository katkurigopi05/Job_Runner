# Notes on the crawler and the registry

Moved here from the root `CLAUDE.md` on 2026-10-11, word for word, so they
load only when work touches this directory. "§N" below is a section of
the root file, which keeps every heading and says where each section went.

## §9, Phase 5: the registry

The registry started at 50. `make validate-seeds` found that 21 of those
returned 404 from both the board API and the rendered page — those companies
have left Greenhouse — and they were removed, taking it to 29. A 404 board is
worse than an absent one: it yields zero postings, which reads identically to
"nothing new since the last poll".

The import of career-ops' company list then added 90 entries, taking it to 119.

**It is now 181 live boards and 14 retired ones**, and this paragraph carried
three wrong claims until someone read the file. It has said "29", then "119",
then "105", and twice described a state of the registry that had already been
superseded — which is the failure mode worth naming here, because a count in
prose goes stale the moment anything writes to the file it describes. It went
stale again the same day, when the OpenHire import below added 76.

What is actually true, read from `seeds/companies.yaml` on **2026-09-13**:

| | |
|---|---|
| live entries under `companies:` | 181 — greenhouse 100, ashby 66, lever 15 |
| entries under `retired:` | 14 |
| stamped `checked` | **105 of 181**, all `2026-09-07`, all state `api` |
| **unstamped** | **76** — the OpenHire import, never verified from here |
| last full sweep | `2026-09-12`, over the 105 then present: all `api=200` |

**Those 76 are the state this section spent three paragraphs warning about**,
and they are here deliberately rather than by oversight. They came from a peer
project that verified each one live, and no machine in this session can reach
an ATS host to confirm it — so they are added *unstamped*, which is the one
honest representation of "somebody else says these work". `make
validate-seeds-write` from the owner's machine is what turns them into
evidence, and until it runs a dead one among them yields zero postings exactly
like a quiet board.

So the two claims this paragraph used to make are both retired with it:

- It said "there is no retired section in that file and never has been". There
  is one now, and it holds what §15 argued for — the evidence rather than a
  deletion. Every entry carries the statuses that condemned it
  (`temporaltechnologies`, `runpod`, `ada`, `lindy`, `factorial`, `vinted` and
  the rest: `404` from the API, and `404` or nothing from the rendered page).
- It said "the 90 imported entries have never been validated", and warned that
  a meaningful share were probably polling nothing. A `--write` sweep on
  2026-09-07 checked **all 105** and every one answered through the board API.
  There are no unvalidated boards left.

The 2026-09-12 re-sweep took just over three minutes for 105 boards, which is
the §2.6 amendment doing its job: every one of these hosts is in
`ratelimit.SHARED_API_HOSTS`, so the floor is 2s rather than 60s. At 60s the
same sweep is 105 minutes, which is the difference between a check you run
before trusting a crawl and one you never run. `api.ashbyhq.com` returned 401
for robots.txt again, which the fetcher logs and treats as it always has.

One gap in what the sweep recorded, since it is the kind of thing that is only
visible later: **every retired entry has `company: None`.** The slug and the
evidence survived the move; the human-readable name did not. A slug that 404s
today may be a rename rather than a departure, and the name is half of what
would tell those apart a year from now.

`make validate-seeds` reports without touching the file;
`make validate-seeds-write` re-stamps. Re-run before trusting a quiet crawl: a
stamp says a board answered on the day it was written, not today, and a board
that has since died yields zero postings — which reads identically to nothing
new since the last poll. That is the whole reason this sweep exists.

## From §15: What the gates do and do not prove

### §2.6 did not hold for `make workers n=4`

The shared host limiter landed with `CRAWLER_SHARED_RATE_LIMITER=false`, on the
reasoning that the crawl was one cycle in one worker and the in-process limiter
was the one with tests measuring it. That reasoning was wrong about its own
codebase, and measuring took four lines:

```text
a = build_fetcher(); b = build_fetcher()
await a.rate_limiter.acquire("boards-api.greenhouse.io")  -> waited 0.000s
await b.rate_limiter.acquire("boards-api.greenhouse.io")  -> waited 0.000s
```

`build_fetcher()` constructs a **new** `HostRateLimiter` on every call. So the
counters were never per-process, they were per-fetcher — and `make workers n=4`
is a documented command that runs four claimants in one process. Four
independent counters, four simultaneous requests to a host owed 2s, measured
0.000s apart. Only `crawl_company_job` passed `shared=True`, so every other
path — discovery, `validate-seeds`, `probe-bespoke`, the inline cycle — ran at
up to 4× the permitted rate.

Every gate passed throughout, and `tests/test_ratelimit_concurrency.py` passed
too, because it builds its limiter explicitly and therefore shares one by
construction. The suite proved the limiter worked and never asked whether
callers got the same one. The two new tests go through `build_fetcher`, which
is the part that was wrong; with the default reverted they report requests
0.000s apart.

The default is now `true`. That makes a reachable database a requirement for
fetching, and an unreachable one fails the fetch rather than falling back to
per-process counting — the right way round, because a fallback here is a breach
nobody would ever see. The table fixes both halves at once: two limiters in one
process and two in separate processes reserve from the same row, so there is no
arrangement of workers that outruns the floor.

### Three thousand careers URLs, and how many are actually a problem

The owner has a CSV of ~3,000 companies with careers URLs.
`scripts/import_portals.py` already stated why they cannot simply be added:

> A company whose careers page is its own site (`twilio.com/careers`) is
> reported and skipped: we have no extractor for a bespoke page, so adding it
> to the registry would mean a crawl cycle that fetches and parses nothing
> every hour, forever.

So the first question is not "how do we scrape 3,000 sites". It is **how many
of them are bespoke at all** — careers URLs very often already *are* a
Greenhouse, Lever, Ashby or Workable board, and each of those needs no new
code: it is a registry row the existing crawler polls first-hand, through the
rate limiter and robots.

`make import-csv src=companies.csv` answers that. Four things are deliberate:

- **Offline.** No network, so 3,000 rows sort in **0.02s** (measured, and
  asserted at that size). A probe per row would make the answer depend on
  which sites happen to be up, and turn a question worth re-asking while
  cleaning the sheet into an hours-long crawl.
- **The remainder is a file, not a number.** Bespoke rows are written to a CSV
  with every original column preserved — a sheet carrying a sector or a
  headcount keeps it, because whatever reads it next may want the biggest
  companies first. A count tells you the size of the problem; the list is what
  the next tool takes as input.
- **"No URL" and "cannot read this page" are separate buckets.** They need
  different fixes, so collapsing them would hide the cheaper one.
- **It never writes to the registry.** `scripts/import_companies.py` appends
  through `import_portals.append_to_registry`, so there is one door. The test
  checks the AST rather than the source text, because the module docstring
  names that function to say it is somebody else's job and a grep would read
  the explanation as the violation.

A link to one posting still yields the board — `board_root` anchors to the
front page on purpose, and a hand-assembled sheet is full of deep links, so
discarding those would count companies we *can* crawl as bespoke, which is the
one number this exists to produce.

### On Scrapling for the bespoke remainder

Recorded because the library is a reasonable idea whose headline feature §2.5
forbids outright. Its README: *"Its fetchers bypass anti-bot systems like
Cloudflare Turnstile out of the box"*, plus a spider with *"automatic proxy
rotation"* that *"backs off when it starts blocking you"*, and
`StealthyFetcher.fetch(...)  # Fetch website under the radar!`. That is
captcha bypass, bot-detection evasion and proxy rotation — three of §2.5's
clauses, and not incidentally: it is the pitch.

Its *parser* is a different matter and is BSD-3, so §3's cost rule is fine. If
it is ever adopted it must be as a parser only, behind `PoliteFetcher`:
Scrapling's fetchers honour neither robots.txt nor our per-host floor, so
letting it fetch turns §2.6 from a control into a comment.

Two further limits worth keeping visible:

- **Adaptive selectors are wrong inside an ATS form.** Its value proposition
  is "if the element moved, find one that looks similar". §2.2 makes
  work-authorization answers verbatim because they have legal consequences,
  and §2.4 says an unanswerable question parks rather than guesses. A drifted
  selector must fail loudly, not find a plausible neighbour.
- **It buys nothing on the boards we already read.** The crawler fetches
  `boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` — JSON. There
  is no HTML to adapt to.

For a bespoke careers page the higher-yield path is **JSON-LD**: career sites
publish schema.org `JobPosting` for Google Jobs, already structured, with no
selector to drift. ~~That is the next build~~ — built, below.

### Reading a bespoke careers page

`packages/crawler/jsonld.py` is that extractor, and `seeds/bespoke_careers.csv`
— the remainder `make import-csv` writes — is its input.

Structured data rather than selectors because career sites publish
`JobPosting` **so that machines read it**: it is what Google Jobs indexes, and
a site that wants its roles found keeps it accurate. That makes it the one
part of a bespoke page with a stable contract. §15 already records what the
alternative costs — a Greenhouse fixture had native `<select>` while the live
board had moved to react-select, and the suite stayed green while the adapter
misread every dropdown. Multiply that by three thousand sites and it is not a
maintenance burden, it is the whole project.

Four things are load-bearing:

- **A page with no JSON-LD yields nothing, and that is the answer.** Not a
  reason to start guessing at markup. The count of pages that yield nothing is
  the measurement that says whether a second strategy — sitemaps, then
  rendered HTML — is worth building, and `make probe-bespoke` is what produces
  that count.
- **`import_portals.py`'s rule survives having an extractor.** It refused
  bespoke pages because "adding it to the registry would mean a crawl cycle
  that fetches and parses nothing every hour, forever". A page that publishes
  no structured data is still exactly that, so `make probe-bespoke` promotes
  only the pages that answered — and a board yielding zero postings reads
  identically to a board with nothing new, which is the failure §9 Phase 5
  already recorded for the 21 dead Greenhouse slugs.
- **A fetch failure is not a verdict.** Blocked by robots, a 403, a timeout:
  each is its own state and none of them promotes or retires anything. A site
  that is down this afternoon is not a site without structured data, and
  `make validate-seeds` treats a `jsonld` seed the same way — it asks whether
  the page still publishes, because a careers page returns HTML and the JSON
  board check would condemn every live one.
- **A `jsonld` seed's slug is its page URL.** The four board APIs derive their
  URL from a company slug; a bespoke page has no such rule, the address *is*
  the identity. Carrying it in `slug` rather than special-casing the crawler
  keeps `(ats, slug)` the de-duplication key it already is in `discover.py`,
  `import_companies.py` and `import_portals.py`. It is deliberately kept out
  of `EXTRACTORS` for the mirror-image reason: callers that iterate that dict
  build a URL from a slug, which is the one question this extractor cannot
  answer.

Nothing here fetches. `PoliteFetcher` gets the bytes, because it is what
enforces robots.txt and the per-host floor (§2.6), and an extractor that
fetched for itself would route around both. The floor costs nothing on this
sweep — thousands of *distinct* hosts, one request each, so no two are ever
serialized behind one counter.

**Not built, and worth stating rather than implying.** There is no sitemap
path: a site that publishes `JobPosting` only on individual posting pages,
with none on the index, still reads as empty. And the sweep needs network
egress from the owner's machine, like `make validate-seeds` — so how many of
the ~3,000 bespoke pages actually publish is, today, an unmeasured number.

## §25: one board is polled under one company row

The owner asked for no duplicates on 2026-10-06, after a crawl. There were
282, and none of the constraints could see them.

Eleven boards were each verified under two company rows: Vercel and ZEIT,
Elastic and Elastic.co, Flexport and Deliverr, Teleport and Gravitational,
Replit and Repl.it. The owner's sheet lists a company under an old name and a
new one, discovery resolved both to the same board, and nothing asked whether
somebody already held it. Each board was fetched twice a cycle and every
posting on it stored twice, same URL and same text: 282 of 11,429 open
postings. The assistant had answered a Kafka question with a Vercel job listed
as "ZEIT". `uq_postings_company_external_id` is per company, and the two
copies belong to two companies.

`packages/crawler/duplicate_boards.py` is the fix, in three places:

- **Discovery asks first.** `holder_of` is consulted before a company is
  verified. A held board sets the newcomer aside only with positive identity
  evidence (see below); otherwise it is an unconfirmed name collision and
  discovery keeps looking. Neither path queues a second crawl for that board.
- **The database refuses the rest.** `uq_companies_verified_board` is a
  partial unique index on `(ats_type, slug)` for verified rows only. Two
  discovery tasks can resolve one board at once, each before the other
  commits, and then neither finds a holder: the index is what still holds.
  The handler writes inside a savepoint and comes out of that with the
  company classified as an alias or a name collision, not with a failed task.
- **`make merge-duplicate-boards` repaired what was there**, after a verified
  backup: 11 rows set aside, 282 copies deleted, 32 scores that only a copy
  had moved to the original, nothing of the owner's touched.

Five things to keep:

- **The row that loses a board is kept.** It becomes `failed`, with no slug,
  carrying what it resolved to and who holds it (`duplicate_board`). The
  registry keeps evidence, not deletions (§9), and the row is the only record
  that the sheet named that company. The earlier implementation also set
  namesakes aside, stopping discovery of their own boards. That is now a
  `name_collision`, retained under `unconfirmed` with its holder and normal
  discovery backoff, rather than the alias's 30-day backoff.
- **The row named like the board keeps it** (`keeper_of`): "Vercel" for
  `vercel`, "Teleport" for `goteleport`. The name is what the owner reads on
  a card. Where the names say nothing, the older row. Discovery cannot wait
  to compare names, so there the first row verified keeps it.
- **A row that loses a board loses its postings to the holder too**
  (`absorb`). Nothing polls a set-aside row, so nothing would ever close
  them, and the holder's next fetch would store each one a second time.
  `sync_registry` does this when a seed names a board a sheet row holds.
- **The index is only on verified rows.** The comment on
  `ix_companies_ats_slug` wanted a collision to stay a fact to look at, and
  it does: hint, unverified and failed rows may name a board any number of
  times.
- **The opening of a savepoint flushes.** The first version changed the row
  and then opened the savepoint, so the change met the index outside it and
  took the whole transaction down. The row is changed inside.

What was left alone, and is a decision rather than a defect: 41 open postings
are an employer listing the same job more than once, with the same title,
place and text but its own requisition id and URL each time (Process Street
has one five times). §16 does not merge within one source or across
requisition ids, and those are the employer's own listings.

## A company not verified is noted, with what its site showed

Added 2026-10-19 at the owner's asking: a company whose careers page is not
found should be noted and fixed where it can be, not retried for ever with
nothing learned.

**Why so many failed.** 3,684 of the 4,251 companies on
`bay_area_tech_companies.csv` were `failed`. Its careers column is 3,797
Google search links, 445 blanks and 9 real URLs, and a search link is never a
lead (`lead_for`; fetching it would be scraping Google, which robots.txt
forbids). So discovery read each company's own website, and recorded only
"its own site names no supported board". A read-only sample of 40 of those
found a careers page for 26: one on a supported board discovery had missed,
five on other job systems, twenty on the company's own page.

**What discovery does now** (`find_boards.from_url`, `resolve_site`):

- **A careers link on the company's own subdomains is followed**
  (`site_of`): `careers.acme.com` and `jobs.acme.com` were skipped by an
  exact-host match.
- **`/careers` then `/jobs` are tried when the site links no careers page**
  (`COMMON_CAREERS_PATHS`), stopping at the first that exists. Each is a
  request to the company's host at the 60s floor, so a company with no link
  costs up to two minutes more of a worker's time. Nothing deeper is guessed.
- **What the careers page is built on is recorded**: another job system by a
  link to its host (`job_systems.py`; Workday, BambooHR, Oracle, Dover and
  more, never by a name in prose), and schema.org JobPostings it publishes.
- **Every failure gets a category** (`SiteReading.category`) and a reason
  that says what was found: `other_job_system`, `structured_jobs`,
  `careers_page_unreadable`, `no_careers_page`, `multi_company_board`,
  `no_website`, `board_empty`, `blocked`, `unreachable`. The careers page, the job system
  and what was tried go into `source_evidence`.

`make discovery-report` lists every failed company with its careers page,
category and fix, as a CSV in `storage/exports/`, and counts how many
companies each job system would unlock. A company that failed before this has
no category until discovery tries it again (`not_yet_rechecked`).

What a later change could undo:

- **`blocked` means a robots.txt that disallows us**, nothing else. A
  robots.txt that could not be fetched is still treated as a refusal
  (RFC 9309), and is filed `unreachable`: on 2026-10-19 that was 678 of 739
  "blocked" companies, nearly all connection errors and timeouts on sites
  that are down or gone, against 61 real refusals.
- **Only what was found is recorded.** An absent field means it was not
  seen, never "none".
- **`resolve_one` is unchanged** for its callers; `resolve_site` returns the
  site's reading beside the outcome.
- **A page naming several companies' boards names none of them**
  (`_read_page`). The first recheck with the code above verified GGV
  Capital on Cyderes's Lever board: GGV's careers link is a Getro board
  listing its portfolio's jobs, and discovery took the first board on it. A
  read of the 79 companies verified from their own site then found Cargo,
  verified 2026-10-05 on whichever of 290 boards its `/jobs` aggregator listed
  first. A board is taken when the page names one company's (any number of
  its jobs, on any number of systems), or, among several, the one whose slug
  is the site's own name. Nothing is taken from a page built on a network
  board system (`job_systems.NETWORK_BOARDS`, Getro). The boards it named go
  into `source_evidence.boards_named`. Sony PlayStation's careers page lists
  its studios' boards and would now fail too; it keeps the board it was
  verified on, because a verified company is not rediscovered.

Not done, each a decision for later: a reader for any of the other job
systems (the report says which would unlock the most companies); taking the
structured jobs a careers page publishes (`jsonld.py` exists, discovery does
not verify a company on it); and pages that build their job list in the
browser, which the fetcher never runs, so a board embedded by JavaScript
reads as a careers page with nothing on it.

## A company a worker cannot read is noted, then looked into

Asked for by the owner on 2026-10-19: when a worker cannot read a company, it
should note it, move on, look into the noted ones once everything else is
done, and put what it cannot fix on a separate list.

- **Noted** (`issues.py`, table `company_issues`, one open row per company
  and stage). Discovery notes every failure but `board_empty`. A crawl notes
  a board after `CRAWL_FAILURES_BEFORE_NOTED` (3) failures in a row:
  `board_gone` (HTTP 404/410), `board_error`, `board_blocked`,
  `board_reads_empty`. Before this, a board that moved was retried every few
  hours for ever; seven were returning 404 that day. A later success closes
  the note (`resolved_by` discovery or crawl).
- **Looked into, with the owner's permission** (`deep_dive.py`, task
  `deep_dive_company`). The owner asked for the deep dive at the end, with
  their permission. Nothing starts one until they say so: `make deep-dive`
  or `POST /company-issues/deep-dive` (`issues.start_deep_dives`), unless
  `DEEP_DIVE_AUTO` gives standing permission, when the tick starts them
  capped by `max_backlog`. The first build started them on every tick; 2,343
  were queued to run overnight unasked, and were cancelled. Once started they
  are claimed only when nothing else is runnable, discovery included. A note
  whose deep dive is cancelled or fails for good is offered again by the next
  start. Once per note otherwise: the same failure again does not reopen one
  the deep dive gave up on; a different failure does.
- **The error list** is the notes the deep dive could not fix
  (`needs_owner`), each with a diagnosis, a fix and `findings.kind`. `make
  company-issues` writes `companies_noted.csv` and `company_errors.csv`.

What it tries is what found something when 53 just-failed companies were read
again by hand that day, and nothing else. Unreachable: the DNS of both forms
of the address, then the other form (2 of 17 answered at `www.`, 3 had no
DNS). A careers page with no board: one page further in (Boeing to Workday),
links to other sites (named, never followed: a board there could be anyone's),
its own job pages, applications by email. No careers page: what the home page
links to. More usual addresses and the sitemap found nothing on 15 sites, so
they are not tried. A known board that stopped answering: the site is read
again for a board it moved to, and a board found is verified through
`discover_company_job.verify_board`, the same path as discovery.

Not done: pages that build their jobs with JavaScript (10 of the 21 careers
pages) say so and wait for a browser step.

## Workday reading and verification (2026-10-20)

Owner approved reading after Phase 6; applying through Workday forms remains
out of scope (§11). `board_root` recognizes `tenant.wdN.myworkdayjobs.com`,
with or without a locale and with deep posting/login links. The identity is
`host|tenant|site`, preserving the site name's case, so the existing verified
board unique index includes tenant, data centre and site. No tenant/site is
guessed. `myworkdaysite.com` links and malformed site names remain unsupported.
Supported Workday links no longer count as `other_job_system`.

Before implementation, robots.txt was read through PoliteFetcher on 23andMe
(wd5), Ameren (wd1) and Atmos Energy (wd108), using `User-Agent: jobrunner`.
All returned 200, allowed their named career-site prefixes and disallowed
`/refreshFacet/`. Neither the list `/wday/cxs/{tenant}/{site}/jobs` nor detail
paths were disallowed. Normal robots checks remain mandatory on every tenant;
a refusal or HTTP 403 is a failure, never an invitation to evade it.

The existing POST path already uses the same robots and host-rate gates as
GET. Tenant hosts remain outside `SHARED_API_HOSTS`: **60 seconds**, unchanged.
At 300 jobs, 15 list pages span at least 14 minutes between first and last
request, plus network time. Fetching 300 details in that cycle would mean
315 requests, at least 314 minutes (5h14m). Instead:

- All list pages each crawl, up to 60; truncation or inconsistent/duplicate
  pagination closes nothing. A page failure abandons the read.
- At most five details per crawl. New/missing or visibly changed listings
  get four slots when cached jobs exist; one rotates through cached jobs to
  detect description-only edits. Once caught up all five rotate. The nullable
  `company_crawl_states.workday_detail_cursor` persists cached validation;
  `workday_pending_cursor` rotates pending jobs independently. Otherwise a
  detail proving a job too old to store could consume the budget on every
  crawl and strand newer jobs behind it.
- A stable 300-job board initially takes about 75 successful crawls
  to hydrate (five first, then four per crawl), about 75 hours only if hourly
  scheduling holds. Once caught up, a full refresh takes 60 crawls. Fleet
  scheduling can extend these lags, the explicit cost of this budget and floor.
  A 15-page + 5-detail crawl spans at least 19 minutes, plus network time;
  the 60-page cap plus five details spans at least 64 minutes.
- Cached text survives unchanged listings. A visible title/location/URL edit
  clears stale text until refreshed. The board hash includes effective posting
  content, so identical listings cannot strand pending or edited descriptions.
  Fully hydrated unchanged content creates no posting versions or match work.
- Until hydration, the existing matcher has title/location only, an empty
  body and no body embedding; requirements remain unstated. Hydration emits a
  normal changed posting for embedding, requirements extraction and rescoring.
  No matcher or feed code changes.
- Exact `startDate` comes from detail. English relative ages are used only
  as lower bounds for `POSTING_MAX_AGE_DAYS`, without inventing a timestamp;
  unknown/localized ages stay unknown until detail. Existing held jobs remain
  subject to the ordinary sighting/closure rules.

`python -m scripts.probe_workday --out /tmp/workday.json` reads the live
inventory in REPEATABLE READ, READ ONLY, then probes one list page per parsed
board for its reported total. It uses one in-memory limiter and disables DB
metrics, so it writes only the output file. Do not run concurrent probes to
the same Workday hosts. The crawler still uses its shared persistent limiter.

The 2026-10-20 read-only run: **120 companies, 111 parse, 106 verify**, one
empty board, four unreachable; nine unparseable (five `myworkdaysite.com`,
four malformed site names). No discovery, company, posting or metrics writes.
Real public Ameren payloads are trimmed under `tests/fixtures/workday`.
The non-empty boards reported 41,668 jobs in total. Eleven exceed the existing
60-page / 1,200-job cap: their crawls expose only the first 1,200 and report
truncation, without closing anything. Verification reports their full API
total, not a claim that a bounded crawl reaches every posting. Increasing
coverage beyond that cap needs a separate crawl-budget decision.

Migration `a6c8e0f2b4d7` adds only two nullable cursors, with no backfill;
downgrade drops only that scheduling state. It was not applied live. After
merge the owner chooses when to migrate and re-discover these companies.

## The company workbook (`make company-sheet`, 2026-10-19)

The owner asked for their CSV's companies split in two: those on a job board
we read, and the rest with their careers pages, as the list to work from.
`make company-sheet` rebuilds `<csv>.xlsx` beside the CSV from the database:
"All companies" (each row, its board, open jobs, how it was found) and "Not
on a job board we read" (careers page from discovery or the deep dive, where
applicants go, what was found, what to do). It is the same set the workers act
on — discovery and the deep dive take exactly the companies with no board — so
rebuilding it after a crawl or a deep dive is what keeps it current. Read-only;
the CSV is never changed. The owner's CSV is git-ignored by name and the
workbook is excluded locally (`.git/info/exclude`): the repo is public.
`openpyxl` is the one dependency it added. `tests/test_company_sheet.py`.

After the Workday reader, `BOARDS` includes Workday and the About board list
comes from that mapping. Sheet 2 stays in the same position with a count-free
name. Unverified companies whose evidence names Workday say "Workday: readable
now, waiting for re-discovery", keeping their Workday URL in "Where to apply".
This only changes the next export; it neither re-discovers companies nor
rebuilds the owner's existing workbook. Tests write temporary files only.

## One discovery task per company (2026-10-19)

`due_for_discovery` skips a company that already has a `discover_company`
task waiting or running. `dispatch_discovery` marks a company taken for one
hour, and with a five-hour queue every tick queued each still-waiting company
again: 3,974 tasks for 2,621 companies that evening, 1,220 of them cancelled
by hand. A duplicate cost a worker a minute of waiting on a site it had just
read, and bumped the company's attempts, pushing its next retry out a step.
The one-hour mark stays, for a worker lost mid-task; it no longer has to be
longer than the queue. `tests/test_discovery_no_duplicates.py`.

## Company identity before suppressing discovery (2026-10-20)

`duplicate_boards.identity_reason` is the automatic discovery/deep-dive gate:
- Same company website domain (excluding shared ATS/search/directory hosts).
- Normalised names overlap **and** the actual board names this company's
  website. `Resolved.confirmed_domain` carries fresh directory evidence;
  historical `confirmed_by: board names the website` counts only for the
  same ATS/slug and lead domain. Slug/name agreement alone does not count.
- A sourced `relationship` record naming both companies: `kind` is `rename`
  or `acquisition`, `from`/`to` name the two parties, `source_url` cites the
  evidence. A previous `duplicate_board` verdict or arbitrary diagnosis prose
  is not a relationship record.

The discovery handler filters occupied candidates while still searching, so a
name collision does not terminate the directory or slug loop. Vendor network
probes still run concurrently with the same robots/rate gates; identity reads
on their shared AsyncSession are serialized. Verification checks identity
again, including after the uniqueness race. Deep dives leave collisions
unresolved instead of closing the issue as fixed. No index/schema change.

The live read-only dry run found 17 set-aside rows: one has sufficient stored
proof (Elastic.co/Elastic); 16 would remain unconfirmed and keep searching,
including Twelve Labs and Charter Communications. Missing proof is not proof
of distinct companies: legitimate aliases also need identity evidence. No
records were repaired or re-discovered; that remains the owner's decision.
The full per-row table is in this change's PR. The explicit legacy
`merge-duplicate-boards` repair/registry absorption is unchanged; this gate
covers automatic discovery and deep dives, not an operator-directed merge.
