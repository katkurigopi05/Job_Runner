# ATS discovery: outside answers on speeding it up

Started 2026-10-04, after the 200-company trial of the Bay Area sheet
(`bay_area_tech_companies.csv`, 3,802 companies). The question below went to
several outside sources; their answers are recorded here word for word, one
section each, before any of them is acted on. Headings inside an answer are
demoted one level so they nest under it; the text itself is unchanged.

Nothing here is a decision. When the owner says to work on them, each answer is
checked against the measured trial and the non-negotiables in `CLAUDE.md`
(§2.5 no evasion, §2.6 per-host floors, §3 zero cost, §11 no extra services) and
only what fits is built.

## The question as asked

```text
I'm building a local, single-user job-search agent (Python) and need ideas to
speed up one step: finding which job board each company uses, for a list of
~3,800 companies. Please suggest approaches that fit my constraints below.

THE PROBLEM
I have a CSV of ~3,800 Bay Area tech companies (name, website, careers-page URL).
For each company I need its job board on Greenhouse, Lever, Ashby or Workable
(their public job APIs), so I can poll those boards for new postings.
Only 8 of the 3,800 careers URLs link straight to a job platform; the rest are
the companies' own careers pages.

Discovery per company runs in two steps:
 1. Fetch the company's careers page and look for an embedded link to a
    Greenhouse/Lever/Ashby/Workable board.
 2. If none is found, guess board names from the company name (~2.45 guesses
    per company) and try each guess against all 4 platforms' public APIs.

Measured on a 200-company trial with 5 workers:
 - 200 companies took 19 minutes (~10.5 companies/min).
 - 33 of 200 (16.5%) resolved to a board. 28 were found in step 1 (careers
   page); only 5 came from step 2 (name guessing).
 - Step 2 is nearly all the wall-clock time: ~10 API requests per company,
   rate-limited per platform. We ran at ~88% of the theoretical ceiling, so
   more workers barely help (the rate limit is per platform, not per worker).
 - Projected for the full list: ~6 hours. Dropping step 2 would lose ~2.5% of
   companies, and I estimate it would finish in under an hour (not yet tested).
 - Once found, polling the boards is fine: 181 boards take 4.5–7 minutes.

ARCHITECTURE
 - Queue: Postgres table, workers claim tasks with
   SELECT ... FOR UPDATE SKIP LOCKED; leases renewed during long tasks; at-least-
   once delivery; handlers are idempotent.
 - Workers: one Python asyncio process, N "claimant" coroutines (default 1, or
   --workers N), each with its own worker id and a heartbeat row in Postgres.
 - Sweep ("dispatch"): a scheduler task runs every 300 s, enqueues up to 250
   per-company tasks (discover_company / crawl_company), caps backlog at 2,000,
   then re-schedules itself. Scoring of new postings runs at the start of each
   sweep tick, not per company.
 - Rate limiting: a shared per-host limiter stored in Postgres (so all workers
   and processes share one counter per host); robots.txt checked and cached
   per host; Crawl-delay and 429 Retry-After honoured.
 - Discovery fans out across the 4 platforms in parallel per company; guesses
   within one platform are sequential.
 - HTTP: httpx async client, 32 max connections, 30 s timeout.
 - Database pool: SQLAlchemy default (5 + 10 overflow = 15 connections per
   process), shared with the rate limiter and heartbeats.

STACK
Python 3.12; FastAPI 0.142 + uvicorn 0.54; Pydantic 2.13; SQLAlchemy 2.0
(async) + asyncpg 0.31; PostgreSQL 16 + pgvector 0.8; Alembic; httpx 0.28;
structlog; Playwright 1.56 (Chromium, only for filling application forms);
sentence-transformers with BAAI/bge-small-en-v1.5 (local CPU embeddings);
PyYAML. Dashboard: Next.js 15.5 + React 19. Docker for Postgres only.
Runs on a MacBook (macOS).

HARD CONSTRAINTS (not negotiable)
 - Runs only on my laptop, single user, no cloud deployment or hosted runtime.
 - Zero recurring cost: no paid APIs, no paid scraping or enrichment services.
 - Polite crawling only: respect robots.txt; at least 60 s between requests to
   the same company website; at least 2 s between requests to each shared
   job-platform API (boards-api.greenhouse.io, api.lever.co, api.ashbyhq.com,
   apply.workable.com). These floors can be raised, never lowered.
 - No captcha solving, no bot-detection evasion, no proxy rotation, no
   fingerprint spoofing.
 - No Redis/Kafka/extra services: the queue stays in Postgres.
 - LLM calls are scarce (free tier, 50 requests/day, shared), so I can't use an
   LLM per company. Running local LLMs makes the laptop sluggish.

WHAT I'M LOOKING FOR
 1. Faster or cheaper ways to map company → job board without guessing
    (public datasets, sitemaps, DNS/CNAME clues, careers-page structures,
    platform directories, Common Crawl, etc.) that fit the constraints.
 2. Better scheduling: e.g. a careers-page-only pass first, guessing only for
    leftovers, caching negative results, prioritising likely matches.
 3. The documented rate limits or terms for the Greenhouse Job Board API,
    Lever Postings API, Ashby public job posting API and Workable's widget API,
    so I know what is actually allowed.
 4. Anything wrong with this architecture for ~4,000 companies on one laptop.
```

## Answers

### 1. DeepSeek

Assuming local, single-user, Python, public ATS APIs, and polite scraping, the fastest design is a **staged pipeline** with async I/O, aggressive caching, and “cheap signals first, expensive browser last.”

#### 1. Normalize + generate strong candidate tokens

For each company, generate a small ordered candidate list (3–10) from:

- domain: `acme.com` → `acme`, `getacme`
- company name: `Acme Corp` → `acme`, `acmecorp`, `acme-corp`, `acme_corp`
- remove legal/industry suffixes: `inc`, `llc`, `ltd`, `corp`, `technologies`, `tech`, `labs`, `group`, `holdings`, `solutions`, `systems`, `software`, `ai`, `io`, `the`
- add common suffixes: `acmeinc`, `acmecorp`, `acmeai`, `acmelabs`, `acmetech`
- careers URL path: `acme.com/careers` → `acme`

Probe in this order: exact domain, name, name-no-suffix, name+suffix.

#### 2. Pass 1: cheap, parallel signals for all 3,800

Run these concurrently with `httpx.AsyncClient` or `aiohttp`:

- **DNS CNAME check** on the careers host. Many `jobs.company.com` / `careers.company.com` hosts CNAME to Greenhouse/Lever/Ashby/Workable.
- **Fetch careers URL**, follow redirects, inspect final URL. Some redirect straight to the ATS.
- **Static HTML extraction** with `selectolax` or regex. Look for:
  - links: `boards.greenhouse.io/{token}`, `jobs.lever.co/{token}`, `jobs.ashbyhq.com/{token}`, `apply.workable.com/{token}`
  - iframes
  - `<meta http-equiv="refresh">`
  - inline JS redirects like `window.location = "..."`
  - CSP `frame-src` headers, which sometimes reveal ATS domains
- **Validate any found token immediately** with the ATS API.

Use 100–200 concurrent requests because these are mostly different domains. Set 5–10s timeouts and limit body reads to ~1 MB.

#### 3. Pass 2: API probing for unresolved companies

For companies still missing an ATS, probe candidate tokens directly.

Endpoints to verify current shapes:

- Greenhouse: `https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`
- Lever: `https://api.lever.co/v0/postings/{token}?mode=json`
- Ashby: `https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true`
- Workable: `https://apply.workable.com/api/v1/widget/accounts/{token}` or `https://www.workable.com/api/accounts/{token}?details=true`

Validation rules:

- HTTP 200 + expected JSON key (`jobs`, `postings`, or top-level array).
- Non-empty jobs = high confidence.
- Empty board can still be valid; store as medium confidence.
- If the API returns a company name, compare it to your target with `rapidfuzz`.

Rate-limit per ATS host. Example: 5–10 concurrent requests to Greenhouse, Lever, Ashby, Workable separately. Use exponential backoff on 429/5xx. Cache 404s as negative results for 30–90 days.

#### 4. Pass 3: fallbacks for the stubborn tail

Only run these for companies still unresolved after Pass 1 + 2.

- Fetch `robots.txt` and `sitemap.xml`; look for `/jobs`, `/careers`, or ATS URLs.
- Fetch the main JS bundle(s) and regex for ATS domains/tokens.
- Use Playwright headless for JS-heavy pages. Capture network requests and iframes. Run 4–8 workers only. This is too slow for all 3,800.
- If allowed, use search-engine queries like `site:boards.greenhouse.io "Company Name"` or Common Crawl index queries. Respect ToS/rate limits.
- Check existing public ATS mapping datasets on GitHub/Kaggle; even stale data gives you candidates to validate.

#### 5. Engineering speedups

- Async first: `asyncio` + `httpx`/`aiohttp`, optionally `uvloop`.
- Parse fast: `selectolax` or regex, not BeautifulSoup for bulk.
- Cache everything in SQLite: URL, status, headers, body hash, fetched_at.
- Store state in SQLite so the job is resumable.
- Use `aiohttp.TCPConnector(limit=200, limit_per_host=10)` and `aiolimiter` for per-host rate limits.
- Retry only 5xx/timeouts, never 404.
- Use `orjson`, `tldextract`, `rapidfuzz`, `tenacity`, `tqdm`/`rich`.
- Output a CSV with `ats`, `token`, `confidence`, `method`, `evidence_url`, `last_checked`.

#### 6. Recommended order

1. Normalize + generate candidates.
2. Pass 1 static/DNS/redirect for all companies in parallel.
3. Pass 2 API probe unresolved candidates with per-ATS rate limits.
4. Pass 3 Playwright/search/sitemap only for the remaining tail.
5. Manually review low-confidence or conflicting results.

The biggest wins are: **don’t use a browser for everything**, **validate tokens with APIs immediately**, **cache negative results**, and **rate-limit per ATS host while parallelizing across company domains**.

### 2. Grok

The bottleneck is not the laptop or the queue. It is **37k name-guess probes against four shared API hosts**, with Ashby often taking ~10s even when you wait only 2s. Invert that: get a board list once, join locally, and only probe leftovers.

#### 1. Invert discovery: board list → company, not company → guess

Name-guessing only found **5/200**. Slugs are often not the company name (`stripe` vs `stripeinc` vs a legal entity). A global slug directory plus a local join beats 2.45 guesses × 4 APIs.

**Use an existing public directory (zero crawl).** [ATS Company Directory, 9,935 boards](https://www.kaggle.com/datasets/umamaheshbandaru/ats-company-directory-9935-public-job-boards) (also on [OSF](https://osf.io/np7xc/overview)), CC BY 4.0, snapshotted Jul–Aug 2026:

| Vendor | Boards | Columns |
|---|---|---|
| Greenhouse | 4,966 | `ats_vendor`, `company_name` (from the board itself), `board_slug`, `last_crawled` |
| Ashby | 2,856 | same |
| Lever | 2,113 | same |
| Workable | not included | — |

Join your 3,800 names against `company_name` + slugified website host. Then **confirm with one API call** (and check the board’s reported name, not just HTTP 200). Do not trust a slug collision.

**Build a fuller list yourself, once, from Internet Archive (free, polite).** This is what [mherzog4/job-boards](https://github.com/mherzog4/job-boards) does (MIT). They reported ~13k *live* boards:

```
ashby       191k archived URLs → 7.5k candidates → 3,617 live
greenhouse  1.3M               → 14k             → 6,797 live
lever       1.3M               → 8.7k            → 2,718 live
```

Method:

1. Query the Wayback CDX API (not Common Crawl — they found CC narrower and flaky):

```
https://web.archive.org/cdx/search/cdx?url=boards.greenhouse.io/*&output=json&fl=original&collapse=urlkey
https://web.archive.org/cdx/search/cdx?url=job-boards.greenhouse.io/*&output=json&fl=original&collapse=urlkey
https://web.archive.org/cdx/search/cdx?url=jobs.lever.co/*&output=json&fl=original&collapse=urlkey
https://web.archive.org/cdx/search/cdx?url=jobs.ashbyhq.com/*&output=json&fl=original&collapse=urlkey
https://web.archive.org/cdx/search/cdx?url=apply.workable.com/*&output=json&fl=original&collapse=urlkey
```

2. Take the first path segment as a candidate slug; drop junk with a shape filter.
3. Validate live boards. Prefer **HEAD on the hosted board HTML** (`boards.greenhouse.io/{slug}`), not GET on the jobs API — a live board returns 200 with an empty body, so you are not downloading 200KB descriptions just to test existence.
4. Cache `boards.json`. Refresh monthly (`--refresh-recent` for last-30-days + [urlscan.io](https://urlscan.io) anonymous search, 30 queries/min, no key).

That list is built **once**. Matching 3,800 companies is then a local fuzzy join (rapidfuzz / pg_trgm), not 6 hours of 404s.

**Common Crawl** is a fallback CDX, not the default. Same idea (`url=boards.greenhouse.io/*`), worse coverage, 503s under load. Use only if Wayback is down.

**Do not use crt.sh to enumerate `%.greenhouse.io` board slugs.** Those are path-based, not per-company certs. CT *is* useful for *your* companies’ own hostnames (`jobs.`, `careers.`, `go.`).

#### 2. Make the careers-page pass actually find embeds

You already get **28/200 from step 1 vs 5 from guessing**. That pass is cheap (different hosts, 60s is per host) and should be the *only* first pass. It is probably missing JS-driven boards.

A static `httpx` GET often never sees `boards.greenhouse.io/...` because the careers page is a React shell. Look in the **HTML you already downloaded**:

| Signal | Example |
|---|---|
| Greenhouse embed script | `boards.greenhouse.io/embed/job_board/js?for={token}` |
| Greenhouse iframe | `boards.greenhouse.io/embed/job_board?for={token}` or `job-boards.greenhouse.io/...` |
| Greenhouse query params | `gh_jid`, `gh_src`, `grnhse_iframe` |
| Lever | `jobs.lever.co/{slug}`, `api.lever.co/v0/postings/` |
| Ashby | `jobs.ashbyhq.com/{slug}`, `api.ashbyhq.com/posting-api/` |
| Workable widget | `apply.workable.com/{slug}`, `apply.workable.com/api/v1/widget/accounts/{slug}` |
| JSON-LD | `JobPosting` / `url` often points at the ATS |
| Next.js | `window.__NEXT_DATA__`, `__ASHBY_*` |
| sitemap | `/sitemap.xml`, robots.txt `Sitemap:` — look for ATS hosts |
| One hop | only follow links whose host is an ATS or the same site |

Also fetch **same-origin** things you already have the right to fetch under robots.txt: `/sitemap.xml`, `/robots.txt`, `/jobs`, `/careers.json`. Do **not** turn Playwright on for 3,800 pages. Use it only when HTML has a “View open roles” button and no ATS signature (a small leftover set).

**DNS/CNAME (free, local, no robots issue):**

```
jobs.{domain}     careers.{domain}     go.{domain}
apply.{domain}    hiring.{domain}      jobs.{domain}
```

Many of these CNAME to `boards.greenhouse.io`, `job-boards.greenhouse.io`, `jobs.lever.co`, `jobs.ashbyhq.com`, `apply.workable.com`. One `asyncio` resolver pass over 3,800 domains is minutes, not hours. Follow with a single GET on the CNAME target to read the slug. crt.sh `%.example.com` is a useful extra for companies that hid `jobs.` from the website.

#### 3. Scheduling: three passes, not one mixed task

Your 6-hour projection is “every company does step 2.” Stop doing that.

```
Pass A  (minutes, local)
  Join CSV vs Kaggle/OSF + your boards.json
  Confirm hits with 1 API GET (no content=true)
  Store negatives

Pass B  (target: <1 hour)
  Careers page + sitemap + DNS for unresolved
  High worker count: hosts are distinct, 60s floor barely binds

Pass C  (overnight, optional)
  Name-guess ONLY unresolved, and only high-prior slugs
  One platform task per company, not one task waiting on all four
  Cache every (platform, slug) → 404 for 30–90 days
```

Prioritise guesses: slugify(domain) first (`stripe.com` → `stripe`), then slugify(name), then name-without-Inc. Drop “Acme Technologies Inc.” → “acmetechnologiesinc” as guess #3 if #1 already 404’d. That is how you get from 2.45 guesses toward ~1.1.

**Split the unit of work.** A `discover_company` that fans out 4 platforms and waits will sit on Ashby’s ~10s floor (see below) while Greenhouse/Lever already finished. Enqueue `discover_careers(company)` and, only if needed, `probe_slug(platform, slug)` as separate jobs. Greenhouse then drains at 2s independently of Ashby.

**Do not raise workers on step 2.** You already ran at 88% of the per-host ceiling. Extra claimants just queue behind the Postgres limiter.

**Enqueue the whole unresolved set.** Sweep-of-250 / backlog-2,000 is right for *ongoing* polling, wrong for a one-shot 3,800-row discovery. The rate limiter *is* the throttle; the queue does not need to drip.

**Keep polling and discovery on separate limiter keys or priorities.** A discovery 429 storm should not delay the 181 boards you already poll in 4.5–7 minutes.

**Negative cache is mandatory.** `(platform, slug) → not_found`, `(company, platform) → no_board`, TTL 30–90 days. Also cache *confirmed* boards and re-verify weekly with one GET, not a full rediscovery.

**Never fetch `?content=true` / `includeCompensation=true` / `details=true` during discovery.** Existence + board display name only. Full descriptions belong in the poller.

#### 4. Documented rate limits and terms

None of the four **public job-board** APIs publish a numeric GET quota for unauthenticated reads. The numbers people quote are almost always the *authenticated customer* APIs. Your 2s floor is stricter than anything they document for GET. Keep it.

| Surface | Auth | Documented GET limit | What they actually say | Intended use |
|---|---|---|---|---|
| **Greenhouse Job Board** `boards-api.greenhouse.io/v1/boards/{token}/...` | none for GET | **none published** | Harvest (private) is ~50/10s with `X-RateLimit-*`. Job Board is “heavily cached”; 429 + `Retry-After` if they throttle. [Job Board docs](https://developers.greenhouse.io/job-board.html) are silent on GET limits. MSA fair-use is for *customers’* Harvest keys, not this public API. | Build *that company’s* careers site |
| **Lever Postings v0** `api.lever.co/v0/postings/{site}?mode=json` | none for GET | **none for GET** | [Official README](https://github.com/lever/postings-api/blob/master/README.md): POST apply is **2 req/s**, 429 if exceeded. Explicit: published jobs “may be scraped by third parties.” Data API v1 (keyed) is 10/s burst 20. Also `api.eu.lever.co` for EU tenants. | Build *that company’s* job site |
| **Ashby public posting** `api.ashbyhq.com/posting-api/job-board/{name}` | none | **none published** | [Public Job Posting API](https://developers.ashbyhq.com/docs/public-job-posting-api) has no rate section. Authenticated `report.generate` is 15/min — do not generalise that. Community measurements: unauthenticated GET often has a **~10s latency floor**; 429s appear under burst; **missing boards can return 200 + empty `jobs: []`**, same as a real empty board. | Populate *your* careers page |
| **Workable widget** `apply.workable.com/api/v1/widget/accounts/{account}` (also `www.workable.com/api/accounts/{subdomain}?details=true`) | none | **none published** (per IP) | Authenticated SPI v3: **10 req / 10s** (account token), **50/10s** (OAuth/partner), 429 + `X-Rate-Limit-*`. [Rate limits](https://workable.readme.io/reference/rate-limits). Widget is a different, unpublished per-IP cap; people do get 429s. Workable’s customer ToS is not a license for bulk widget harvest, but the widget is the endpoint their own help article tells you to curl. | Embed jobs on *that* careers page |

Practical implications for you:

- **Ashby is the time sink**, not Greenhouse. Per-host 2s is irrelevant if the server holds the connection ~10s. Give Ashby a 30–45s timeout (your 30s is on the edge), **never parallelise Ashby**, and treat `200 + jobs: []` as “unknown”, not “found empty board”, unless the HTML board page exists.
- **HEAD vs GET:** fine for hosted board HTML. For JSON APIs, a cheap GET without `content=true` is the existence check; don’t assume HEAD is implemented.
- **Verify identity:** Greenhouse `GET /v1/boards/{token}` returns `{name, content}`. Lever/Ashby/Workable payloads include a company name. A 200 on slug `meta` is not Meta.
- Identify yourself (`User-Agent` + contact). Honour `Retry-After`. That is what “polite” means when they publish no GET number.
- These APIs are meant for *one company’s careers page*, not a census. A one-time directory join plus rare confirmation GETs is much closer to that than 37k sequential probes.

#### 5. Architecture vs ~4,000 companies on one MacBook

The design is sound for a laptop. A few things will hurt you at this size:

**What is already right**

- Postgres `FOR UPDATE SKIP LOCKED`, leases, idempotent handlers, at-least-once.
- Shared per-host limiter in Postgres (no Redis).
- robots.txt + Crawl-delay + 429 Retry-After.
- httpx async, 32 connections.
- Not using Playwright for discovery.
- Not using the LLM per company.

**What I would change**

1. **Do not hold the rate-limit row while you sleep.** `UPDATE counters SET next_ok = ... RETURNING wait_ms` in a short transaction, then `asyncio.sleep` outside it. Holding `FOR UPDATE` for 2–10s serialises every other worker behind a row lock.
2. **SQLAlchemy default pool (5+10) is tight.** Heartbeats, limiter, claims, and N claimants share it. Set `pool_size ≈ claimants + 4`, `max_overflow` small, `pool_pre_ping=True`. Recycle connections. One process is enough; don’t run two copies against 15 connections.
3. **Default `--workers 1` is correct for step 2, wrong for step 1.** Make worker count a per-task-type setting: e.g. 8–16 for careers-page fetches (different hosts), 1 producer per ATS host for probes.
4. **Per-host timeouts.** Greenhouse/Lever should be 5–8s; Ashby 30–45s. A 30s timeout on a hung Greenhouse call wastes a worker.
5. **Don’t mix scoring into the discovery sweep.** Scoring at the start of every 300s tick is fine for steady state, not while you are draining 3,800 discover jobs.
6. **sentence-transformers on CPU while crawling** will steal cores from asyncio’s thread pool (DNS, gzip). Pause embeddings until discovery is done.
7. **False positives from guessing will pollute polling.** Require name/domain agreement, or at least one overlapping token, before you insert a board. A wrong slug that happens to be a real other company will drip junk jobs forever.
8. **16.5% is a coverage number, not a speed number.** For a “Bay Area tech” list, many leftovers are Workday, Rippling, SmartRecruiters, Phenom, Greenhouse-with-an-unguessable-token, or no public board. Faster guessing will not move that much. A directory join + better HTML signatures will. If you later want yield, SmartRecruiters (`api.smartrecruiters.com/v1/companies/{id}/postings`) is the next *free, public, keyless* API; Workday is not.

**What not to add:** extra services, a second Postgres, Redis, proxies, a local LLM for slug generation. The queue is not the problem.

#### What I would actually run this week

1. Download the Kaggle/OSF 9,935-row file. Fuzzy-join on name + registrable domain. Confirm matches with one lightweight GET each. Expect this to capture most of the 16.5% you already find, in minutes.
2. One Wayback CDX pull for the four ATS hosts (plus Workable). Shape-filter, HEAD-validate at 2s/host, save `boards.json`. Join again. This is the step that finds unguessable slugs.
3. DNS CNAME + sitemap + embed-script regex on the remaining careers URLs. High parallelism.
4. Name-guess only the residue, domain-derived slug first, platforms as independent jobs, negatives cached. Optionally skip Ashby guesses entirely until the residue is small.
5. Drop step 2 from the default path. Re-run it quarterly, not on every sweep.

If you want, paste (redacted) the 200-row trial columns you already have — especially how step 1 detects a board today, and a few careers URLs that failed both steps — and I can sketch concrete parsers/regexes and a pass-A SQL join against that Kaggle schema.

### 3. Gemini

#### 1. Zero-Cost Discovery Without Name Guessing

Step 2 is slow because your ATS API rate limiter (2 seconds/request) forces serial execution against shared hosts. The solution is to resolve the company to an ATS *before* touching the ATS public APIs, using zero-cost methods that do not share those rate limits.

```
+-------------------------------------------------------------------------------+
| Company Input (~3,800 domains)                                                |
+-------------------------------------------------------------------------------+
                                     |
                                     v
+-------------------------------------------------------------------------------+
| Pass 0: Local Seed / Open-Source Repos (Offline, 0 requests)                  |
| - Community ATS lists (SimplifyJobs, PittCSC, open job scrapers)              |
+-------------------------------------------------------------------------------+
                                     | (if unmapped)
                                     v
+-------------------------------------------------------------------------------+
| Pass 1: Async DNS CNAME Probes (Zero HTTP requests, ~15 seconds total)        |
| - Check CNAME for jobs.{domain}, careers.{domain}, talent.{domain}            |
+-------------------------------------------------------------------------------+
                                     | (if unmapped)
                                     v
+-------------------------------------------------------------------------------+
| Pass 2: HTTP HEAD & Deep Careers Page Regex (Parallel across 3,800 domains)   |
| - Fast HEAD check on redirects                                                |
| - Full HTML regex: <script>, <iframe>, and embedded JS config objects        |
+-------------------------------------------------------------------------------+
                                     | (if unmapped, ~5-8% remaining)
                                     v
+-------------------------------------------------------------------------------+
| Pass 3: Dedicated ATS Candidate Queues (Rate-limited, 2.0s floor)             |
+-------------------------------------------------------------------------------+

```

##### A. Async DNS CNAME Probes (Zero HTTP Traffic)

Most tech companies map `jobs.company.com` or `careers.company.com` to their ATS via a DNS CNAME record.

* **Greenhouse:** points to `boards.greenhouse.io` or `gh-boards.greenhouse.io`
* **Lever:** points to `cname.lever.co` or `jobs.lever.co`
* **Ashby:** points to `custom.ashbyhq.com` or `jobs.ashbyhq.com`
* **Workable:** points to `custom.workable.com`

Using `aiodns` or `dnspython`, you can probe `jobs.{domain}`, `careers.{domain}`, and `talent.{domain}` asynchronously:

* DNS queries hit your local recursive resolver (or 1.1.1.1/8.8.8.8), not the company's web server.
* They bypass both the 60-second website delay and the 2-second ATS API rate limit.
* **Speed:** 3,800 domains $\times$ 3 subdomains can resolve in **10–15 seconds total** across an async event loop.

##### B. HTTP `HEAD` Redirect Probing

Many companies don't host a custom careers page at all; `careers.company.com` simply issues a `301/302` redirect straight to `[https://jobs.ashbyhq.com/](https://jobs.ashbyhq.com/){slug}` or `[https://boards.greenhouse.io/](https://boards.greenhouse.io/){slug}`.

* Send an async `HEAD` request to `https://careers.{domain}` with `follow_redirects=False`.
* Inspect the `Location` header. If it matches an ATS domain, parse the board slug directly from the URL.

##### C. Deep Careers Page Regex (Eliminating Step 1 Misses)

In your trial, 5 out of 33 companies were missed by Step 1 because modern careers pages embed ATS widgets dynamically rather than exposing simple `<a href="...">` links. Instead of just searching anchor tags, run a single regex pass over the raw HTML body for script tags, iframes, and hydration state:

| Platform | What to Search For in Raw HTML | Slug Extraction Pattern |
| --- | --- | --- |
| **Greenhouse** | Script embed / iframe / data attr | `boards.greenhouse.io/(?:embed/job_board?for= |
| **Lever** | Script embed / container hook | `api\.lever\.co/v0/postings/([a-zA-Z0-9_\-]+)` or `data-site=['"]([a-zA-Z0-9_\-]+)['"]` or `jobs\.lever\.co/([a-zA-Z0-9_\-]+)` |
| **Ashby** | Embed script / iframe / Next.js JSON | `jobs\.ashbyhq\.com/([a-zA-Z0-9_\-]+)` or `api\.ashbyhq\.com/posting-api/job-board/([a-zA-Z0-9_\-]+)` |
| **Workable** | Widget script / iframe / config var | `apply\.workable\.com/([a-zA-Z0-9_\-]+)` or `whr_account\s*=\s*['"]([a-zA-Z0-9_\-]+)['"]` or `apply\.workable\.com/api/v1/widget/accounts/([a-zA-Z0-9_\-]+)` |

Searching raw HTML for these patterns resolves the vast majority of "hidden" boards in Step 1 with zero extra HTTP requests.

##### D. Pre-load Open ATS Datasets (Static L1 Cache)

Open-source job aggregation repositories (such as the community lists maintained in internship/new-grad trackers like PittCSC and SimplifyJobs, or open-source ATS scrapers on GitHub) maintain pre-compiled CSVs/JSONs mapping thousands of US tech company domains to their Greenhouse/Lever/Ashby slugs.

* Ingesting one of these public static dumps into a Postgres table gives you an instant, zero-request match for 15% to 30% of your Bay Area company list.

---

#### 2. Better Scheduling & Pipelining

The primary bottleneck in your run was running Step 1 and Step 2 synchronously within the same task. If a company missed Step 1, the worker blocked on up to 10 sequential API calls throttled to 2 seconds each (20 seconds of idle worker time).

##### Separate Fast-Path Discovery from Slow-Path Guessing

Split your workflow into staged passes rather than handling each company end-to-end:

1. **Pass 1 (Wide Fan-out Crawl):**
* Run careers-page crawling and deep regex across all 3,800 companies.
* Because your 60-second politeness constraint is **per company domain**, and all 3,800 domains are distinct, you can maximize your 32 `httpx` connections.
* At ~1.5s per response across 32 connections: $\frac{3,800 \times 1.5}{32} \approx 178\text{ seconds}$ (~3 minutes).
* Expected yield: ~85–90% of all valid boards resolved.


2. **Pass 2 (Residual Guessing Queue):**
* Only the remaining ~400–500 unmapped companies ever enter Step 2.
* Instead of a worker querying all 4 platforms per company, invert the queue: schedule tasks **by platform**, not by company.
* Create 4 dedicated async queues: `q_greenhouse`, `q_lever`, `q_ashby`, `q_workable`.
* Each platform gets a single dedicated loop running at exactly 1 request per 2.0 seconds.


3. **Prioritize Bay Area ATS Platforms:**
* For Bay Area tech startups, ATS usage is heavily skewed: Greenhouse and Ashby dominate modern venture-backed startups, followed by Lever. Workable is comparatively rare.
* Query Greenhouse and Ashby first. If a match is found on Greenhouse, cancel the remaining pending checks for Lever, Ashby, and Workable for that company.


4. **Negative Caching:**
* Store every checked `(platform, slug, status_code)` tuple in Postgres.
* If two different companies generate the same slug guess (e.g., `zenith` or `apex`), the second company hits the local negative cache instead of wasting a 2-second rate-limit slot.



---

#### 3. Documented Rate Limits & ATS Terms

| Platform | Official Endpoint Pattern | Auth | Documented Rate Limits | Notes & Terms |
| --- | --- | --- | --- | --- |
| **Greenhouse** | `GET [https://boards-api.greenhouse.io/v1/boards/](https://boards-api.greenhouse.io/v1/boards/){token}/jobs` | None (Public) | **None published** for Job Board API. (Harvest API is 50 req/10s). | Served through Fastly/Cloudflare CDN. Safe courtesy floor is 2–5 req/sec per IP; 2.0s is conservative. |
| **Lever** | `GET [https://api.lever.co/v0/postings/](https://api.lever.co/v0/postings/){site}?mode=json` | None (Public) | **None published** for public v0 Postings API. (Partner v1 API is 10 req/s). | Intended for custom careers site feeds. CDN-cached; returns JSON array. |
| **Ashby** | `GET [https://api.ashbyhq.com/posting-api/job-board/](https://api.ashbyhq.com/posting-api/job-board/){name}` | None (Public) | **None published** for Posting API. (Reporting API has 15 req/min). | Token name can be case-sensitive depending on edge routing. Built for customer careers widgets. |
| **Workable** | `GET [https://apply.workable.com/api/v1/widget/accounts/](https://apply.workable.com/api/v1/widget/accounts/){token}` or `/api/v3/accounts/{token}/jobs` | None (Public widget) | **10 requests per 10 seconds** (1 req/s) documented for API accounts. | Returns HTTP 429 when burst threshold is exceeded. Your 2.0s floor (0.5 req/s) stays well within this. |

---

#### 4. Architectural Review for ~4,000 Companies on macOS

Your stack (Postgres `SKIP LOCKED`, `asyncpg`, `httpx`, macOS) is well-suited for this scale, but there are four specific bottlenecks and risks to address:

##### A. Database Connection Starvation During Long HTTP Awaits

* **The Issue:** Your SQLAlchemy pool is `5 + 10 = 15`. If a coroutine checks out an async database session, starts a transaction, and then performs `await client.get(...)` (with a 30s timeout and a 2s rate limit delay), that connection is locked and unusable by other coroutines.
* **The Fix:** Ensure absolute separation of I/O phases. A worker must claim a task with `SKIP LOCKED`, immediately commit and close the DB transaction, perform all network operations, and only check out a new connection to record the result. Never hold an open connection across an HTTP await.

##### B. Postgres-Backed Rate Limiting is Unnecessary Overhead

* **The Issue:** Querying Postgres on every single request or tick to manage a shared rate-limiter adds round-trip latency, connection pool pressure, and row contention.
* **The Fix:** Because you run a **single Python asyncio process** on one laptop, shared host rate-limiting belongs in application memory. Use an in-process token bucket or leaky bucket (such as `aiolimiter.AsyncLimiter` or a simple `dict[str, float]` storing `last_request_time` protected by an `asyncio.Lock`). It executes in sub-microsecond time with zero database I/O.

##### C. macOS Default File Descriptor Limit (`EMFILE`)

* **The Issue:** macOS defaults `ulimit -n` to **256** in many terminal shells.
* **The Fix:** With 32 `httpx` sockets, database pool connections, file logs, and Python process handles, you will quickly trigger `OSError: [Errno 24] Too many open files`. Add `ulimit -n 4096` to your shell startup (`~/.zshrc`) or launcher script.

##### D. Persistent `httpx.AsyncClient` Lifecycle

* Keep a single `httpx.AsyncClient` open for the lifetime of the process. Instantiating clients per task destroys connection pooling and leads to socket accumulation in `TIME_WAIT`. Configure socket limits explicitly:
```python
limits = httpx.Limits(
    max_connections=40,
    max_keepalive_connections=20,
    keepalive_expiry=30.0,
)

```

### 4. ChatGPT (Astra)

**Move guessing out of the main discovery pass.** Your measurements suggest the biggest improvement will come from reducing requests and scheduling deferred work better.

Careers-page extraction produced **28/33 ≈ 85% of your successful discoveries**. Guessing recovered only **5/172 ≈ 2.9% of the remaining companies**. That makes guessing suitable for a low-priority background queue. One qualification: dropping it loses 2.5 percentage points of company coverage, but approximately **15% of the boards you successfully discovered**.

Here is what I would change, in priority order.

**Start by testing existing board directories against your CSV.** I found two concrete, downloadable sources:

| Source | Published coverage | How I would use it |
|---|---|---|
| [ATS Company Directory on Figshare](https://figshare.com/articles/dataset/ATS_Company_Directory_9_935_Companies_Mapped_to_Greenhouse_Lever_and_Ashby_Public_Job_Boards/33154145) | 9,935 entries across Greenhouse, Lever and Ashby. Fields include company name, vendor, slug and crawl date. CC BY 4.0. | Import once; match names locally; validate promising candidates. Its observations date from July–August 2026. |
| [Job Board Directory](https://kalebconfer-sys.github.io/job-board-directory/) | Publisher reports 12,122 boards across eight platforms, including your four. Downloadable CSV/JSON with board and API URLs. | Particularly useful for Workable coverage. The publisher says the files are free without signup. |

These are publisher-reported datasets, not coverage I independently verified against your companies. The Figshare publisher explicitly warns that boards disappear and names are not normalized; the second directory similarly describes its results as observations that can become stale. [Figshare](https://figshare.com/articles/dataset/ATS_Company_Directory_9_935_Companies_Mapped_to_Greenhouse_Lever_and_Ashby_Public_Job_Boards/33154145?utm_source=chatgpt.com)

Perform the matching locally, using normalized names, known aliases and website-derived names. **Treat a name match as a candidate, not proof of ownership.** A successful API response proves that a board exists; it does not prove that it belongs to your company. Confirm through the company’s careers link, a matching company website exposed by the board, or other corroborating evidence.

Deduplicate validation globally by `(platform, region, board_slug)`. When validation returns jobs, retain that response as the first poll instead of immediately fetching it again.

**Make the first HTML fetch extract more than links.** If your current extractor mostly checks `<a href>`, expand it to inspect:

- Redirect destinations, iframe sources, script sources and form actions.
- Inline configuration, `data-*` attributes and embedded JSON.
- Framework hydration data and JSON-LD.
- Escaped URLs inside strings, including HTML entities and `\/`.
- Individual job links, which often contain the board identifier even when there is no board-homepage link.

Greenhouse explicitly supports embedded boards whose tokens appear in careers-page HTML; its current integration documentation describes script and iframe embedding. Workable also provides an embedded widget. These are worthwhile extraction targets without executing JavaScript. [Greenhouse Support](https://support.greenhouse.io/hc/en-us/articles/46365908766875-Embed-a-Greenhouse-job-board-on-your-career-site?utm_source=chatgpt.com)

Recognize both older and current platform URL forms. For example, Greenhouse documentation contains `boards.greenhouse.io`, while its current board-token documentation uses `job-boards.greenhouse.io`. Lever documents separate global and EU postings endpoints; retain that region when an observed link identifies it instead of trying both regions for every company. [Greenhouse](https://docs.greenhouse.io/job-board.html?utm_source=chatgpt.com)

Also separate these outcomes:

- Supported platform and exact board identified.
- Supported platform detected, identifier missing.
- Another ATS positively identified.
- No evidence found.
- Fetch failed or access blocked.

A clear Workday link should ordinarily remove that company from four-platform guessing. A timeout should not.

For leftovers, follow at most one or two **observed, promising resources**: an “Open positions” link, a careers-specific script, or a relevant sitemap. Schedule each fetch under the same website delay. Avoid downloading every JavaScript bundle.

**Use sitemaps and DNS as supporting clues; use Common Crawl selectively.**

| Method | Practical value under your constraints |
|---|---|
| Sitemap URLs already present in cached `robots.txt` | Cheap way to identify careers/job paths. Fetch only relevant sitemap branches; cap depth and size. |
| CNAME lookup for the known careers hostname | May identify a provider. Usually insufficient to identify its board slug; a generic CDN target proves little. |
| Company-name search on vendor marketing/customer pages | Weak coverage and may identify a customer without revealing its board. Low priority. |
| Common Crawl | Useful for recovering previously published links from unresolved careers pages. More setup than importing a directory. |

Workable officially supports careers pages on company-owned subdomains, so a company-domain careers URL does not imply a custom-built recruiting system. [help.workable.com](https://help.workable.com/hc/en-us/articles/4405165933591-Can-I-have-my-careers-page-hosted-on-my-own-website-custom-domain?utm_source=chatgpt.com)

For Common Crawl, query a small number of recent indexes for the **known careers URL**, then retrieve the captured HTML using the returned WARC filename, offset and length. Its documentation provides an HTTP range-request workflow, which can run entirely on your laptop. **The URL index itself does not supply the page’s outgoing links**; those require reading the archived content. [commoncrawl.org](https://commoncrawl.org/cdxj-index?utm_source=chatgpt.com)

Cache results and use this only for selected leftovers initially. Common Crawl explicitly asks clients not to overload its index server and recommends other approaches for bulk filtering. Scanning whole ATS domains across years of indexes could easily become another expensive discovery project. [index.commoncrawl.org](https://index.commoncrawl.org/?utm_source=chatgpt.com)

I would not add browser rendering to the default discovery path. Besides laptop overhead, page navigation can automatically issue many same-host requests, conflicting with your strict 60-second website floor unless every request is controlled.

**Change discovery into a staged, resumable process.** My suggested order is:

| Stage | Work | Priority |
|---|---|---|
| 1 | Reuse verified mappings; import and match directory candidates | Highest |
| 2 | Fetch and comprehensively parse careers pages | High |
| 3 | Validate exact identifiers found through evidence | High |
| 4 | Follow bounded careers links/scripts/sitemaps; selective archive lookup | Medium |
| 5 | Try ranked name guesses for unresolved companies | Low |

Give guessing a **request budget**, rather than allowing every company to consume roughly ten requests automatically. Rank candidates using observed vendor clues, exact directory matches, domain-derived names and the historical success rate of each guess transformation. No LLM is needed.

If a page identifies Ashby but omits its slug, try plausible Ashby candidates first. Persist the candidate list and attempted results so a retry does not repeat earlier calls. After a verified match, stop pending speculative alternatives; still allow multiple boards when explicit evidence identifies regional or subsidiary boards.

Suggested cache policies—not vendor requirements—are:

| Result | Suggested handling |
|---|---|
| Verified mapping | Keep it while polling succeeds; rediscover on evidence of migration |
| Valid, identified board with zero jobs | Retain the mapping |
| Definitive nonexistent candidate | Cache for 7–30 days |
| Successful page fetch, no board evidence | Revisit after 14–30 days or a relevant content change |
| Timeout, 5xx or 429 | Transient failure with bounded backoff |
| Robots denial or persistent access block | Separate blocked state; do not convert to “company has no board” |

An empty response alone may be ambiguous: do not use it to establish board ownership or nonexistence without checking that platform’s behavior.

**Your dispatcher creates a separate throughput ceiling.** Starting with an empty queue and dispatching the first batch immediately:

\[
\lceil 3800/250\rceil=16\text{ batches}
\]

At five-minute intervals, the last batch is dispatched at **minute 75**, before its work finishes. Therefore, a sub-hour full pass is impossible through that dispatch path as described.

Keep the 2,000-task backlog cap, but refill when the backlog falls below a low-water mark, or add a bootstrap dispatcher that refills more frequently. Separate refill scheduling from scoring so more frequent dispatch does not repeatedly trigger expensive scoring.

Also make delayed work release its claimant:

```text
Host unavailable → persist next eligible time → release task
Host eligible    → claim → acquire permission to send → fetch → persist result
```

This matters especially for cold robots caches. Under your strict rule, fetching `/robots.txt` and then the careers page entails a 60-second gap. If five claimants all wait through that gap, they cannot advance other companies. **More concurrency can help the careers-page pass**, even though it cannot materially improve saturated platform guessing.

**The official API documentation does not establish a universal public-read rate allowance.** Checked against the documentation available on October 4, 2026:

| Platform | Public read interface | What I could verify about limits |
|---|---|---|
| Greenhouse | `GET /v1/boards/{board_token}/jobs` | GET endpoints explicitly require no authentication. I found no numeric Job Board GET limit in this documentation. Harvest limits belong to a different API. [Greenhouse](https://docs.greenhouse.io/job-board.html?utm_source=chatgpt.com) |
| Lever | `GET /v0/postings/{site}?mode=json` | The Postings documentation publishes **2 application POST requests/second**, subject to change. That is not a documented GET-postings allowance; I found no numeric GET quota there. [GitHub](https://github.com/lever/postings-api?utm_source=chatgpt.com) |
| Ashby | `GET /posting-api/job-board/{name}` | The public posting guide documents this interface but gives no numeric request limit. Do not borrow limits from its authenticated recruiting API. [developers.ashbyhq.com](https://developers.ashbyhq.com/docs/public-job-posting-api?utm_source=chatgpt.com) |
| Workable | Documented public route: `https://www.workable.com/api/accounts/{subdomain}` | Official documentation and support identify this public jobs route. I did not find a numeric unauthenticated-read quota or an official rate contract specifically for `apply.workable.com/api/v1/widget/accounts/{slug}`. [workable.readme.io](https://workable.readme.io/reference/jobs-1?utm_source=chatgpt.com) |

Workable’s published limits are **10 requests/10 seconds for account tokens** and **50/10 seconds for OAuth or partner tokens**. They are scoped to authenticated clients and should not be presented as the widget’s allowance. [workable.readme.io](https://workable.readme.io/reference/rate-limits?utm_source=chatgpt.com)

For terms, the official starting points are [Greenhouse’s legal center](https://www.greenhouse.com/legal), [Lever’s terms](https://www.lever.co/legal/terms-of-service), [Ashby’s customer terms](https://www.ashbyhq.com/resources/terms), and [Workable’s terms](https://www.workable.com/terms). These materials principally describe customer/service relationships; they do not give me a clear, blanket authorization for your particular cross-company polling workload. Public API documentation establishes intended programmatic access, but **“no published GET limit found” does not mean unlimited access or unconditional permission**. [Terms of service](https://www.greenhouse.com/legal?utm_source=chatgpt.com)

Keep your existing floors, increase delays when required, and share cooldowns across discovery and polling. If using Workable’s documented route and following redirects, check robots and rate eligibility at each destination; do not treat alternate hostnames as extra capacity.

**The architecture is suitable for this scale.** PostgreSQL explicitly identifies queue-like tables as a use case for `SKIP LOCKED`. You do not need another queue service for 4,000 companies. [postgresql.org](https://www.postgresql.org/docs/16/sql-select.html?utm_source=chatgpt.com)

The implementation details I would inspect are:

- **Transaction lifetime:** commit task claims promptly. Never hold a database connection or row lock while waiting for HTTP, a rate slot or a retry.
- **Async sessions:** each concurrent database task needs its own `AsyncSession`; sharing an engine/pool is different from sharing a session. This applies to your four-platform fan-out and heartbeats. [SQLAlchemy 2.0 Documentation](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html?utm_source=chatgpt.com)
- **Strict spacing:** ensure the limiter enforces minimum gaps between actual requests. A fixed-window counter can allow boundary bursts; expired reservations must not bunch together after laptop sleep.
- **Lease ownership:** completion and renewal should check the current lease token/generation. An expired worker must not complete a task subsequently claimed by another worker.
- **Scheduling fairness:** reserve capacity for known-board polling. Speculative discovery should not consume all platform slots or fill the entire backlog.
- **CPU work:** keep embedding/scoring off the asyncio event loop and bound its batches so it cannot stall heartbeats.
- **Deduplication:** prevent duplicate pending discovery/poll tasks, and share polling for companies that map to the same board.

Your 15-connection database pool is not inherently too small: HTTP concurrency does not require an equal number of database connections when transactions are short. Measure pool checkout waits before increasing it.

For the next benchmark, reuse the same 200 companies and compare **directory-assisted discovery plus improved HTML extraction, with guessing disabled in the foreground**. Measure verified boards, false matches, requests per additional board, cold/warm robots behavior, and time spent waiting for dispatch versus rate limits versus HTTP. Keep the five guessing-only successes as a regression set. That will show whether the faster pass preserves those valuable recoveries before you run all 3,800.

### 5. Claude

The arithmetic first: step 2 is bound by your own 2 s floor times the number of probes, so more workers can't help. You're spending roughly 32,000 probes (about 3,200 companies × ~10) to find about 95 boards, or about 340 probes per hit. Nearly all the speedup has to come from sending fewer probes. There's also a dispatcher ceiling (section 4) that will cap even a careers-page-only pass at about 75 minutes.

#### 1. Mapping company → board without guessing

**Build an offline slug index from Common Crawl.** This is the biggest win. Board URLs on these platforms are easy to recognize: `boards.greenhouse.io/{t}`, `job-boards.greenhouse.io/{t}`, `jobs.lever.co/{s}`, `jobs.ashbyhq.com/{n}` and `apply.workable.com/{a}`. In Common Crawl's index, each platform's URLs sort together under its reversed-host (SURT) prefix, such as `co,lever,jobs)/`. A few range requests per crawl can therefore pull every slug the crawler has seen.

Don't use the public query endpoint for this. Common Crawl says its CDX API is frequently abused and heavily rate limited, and clients that send too many requests can have their IP temporarily blocked. Use the files instead. For bulk work they point people to each crawl's cluster.idx, which holds one out of every 3,000 index records, sorted by SURT, and the index shards are downloadable from data.commoncrawl.org. Look up your five prefixes in cluster.idx, fetch only the matching byte ranges of the `cdx-*.gz` shards, and decompress each block (ZipNum blocks decompress independently). Repeat for the last 4–6 crawls.

Then match slugs against normalized company names and domain stems locally (exact match first, then rapidfuzz). Spend one API call verifying each candidate. Greenhouse's `GET /v1/boards/{board_token}` returns just the organization's name and board content, so it's both the cheapest probe and a way to reject same-slug false positives. That turns about 10 probes per company into about 1 per matched candidate.

**Add other free seed sources to the index:**
- The Wayback Machine's CDX API (gently).
- HN "Who is Hiring" comments via the free HN Algolia API, searching for those hostnames.
- Existing open-source harvesters. One project, mtdiedrich/ashby, collects slugs from Wayback, HN, GitHub and Common Crawl, can also guess from the YC directory, and validates every slug before keeping it. There's also ConorsCode/open-jobs-data, a free daily-updated dataset of engineering jobs across nine ATS platforms. Check licenses before reusing their lists.

For Workable specifically, a third party describes jobs.workable.com/api/v1/jobs as Workable's own search across all its customers, paginated at 20 jobs per page. That's effectively a directory, but it's undocumented as an API, so treat it as best-effort.

**Widen the careers-page signatures.** Also match:
- `boards-api.greenhouse.io/v1/boards/{t}` and `embed/job_board?for={t}` (and the `/js?for=` variant)
- `api.lever.co/v0/postings/{s}`
- `api.ashbyhq.com/posting-api/job-board/{n}`
- `workable.com/api/accounts/{a}` and legacy `{a}.workable.com`
- Lever's EU hosts: some accounts live in the EU region and answer only on api.eu.lever.co, with boards on `jobs.eu.lever.co`. Your limiter floors don't currently cover that host.

A `gh_jid=` parameter in links means "Greenhouse, token unknown," which narrows guessing to one platform. For JS-rendered careers pages, grep the first-party JS bundles (and `__NEXT_DATA__`) for the same patterns. Each bundle fetch counts against your 60 s floor, so schedule them as deferred requests rather than waiting on them.

**DNS is nearly free.** Resolving `careers.` and `jobs.` subdomains for 3,800 domains takes about 7,600 DNS queries and never touches company servers. CNAME targets sometimes reveal the vendor. The yield is unknown, so test it on your 200-company trial and keep it only if it finds anything.

#### 2. Scheduling

Run the careers-page pass over everything first. Then classify each company's outcome instead of sending every miss to guessing:

- **Board found:** done.
- **Platform known, token unknown:** guess on that one platform only.
- **Other ATS detected** (Workday, iCIMS, SmartRecruiters, Jobvite, BambooHR, Rippling, Recruitee, and so on): skip guessing entirely, since they won't be on your four platforms.
- **Dead site or no careers page:** skip, or put at lowest priority.
- **Ambiguous:** check against the offline index, then send to blind guessing.

Your 16.5% hit rate suggests many companies use other ATSes or have no board at all, so that gate alone may remove most of step 2.

Make the remaining guesses one task per (company, platform, guess rank) and run them breadth-first: every rank-1 guess across all companies before any rank-2 guess. Check which rank produced your 5 trial hits. If they were all rank 1, this finds most hits in the first ~40% of probe time, and you can drop the guess rules that never hit. Order platforms by your observed hit distribution too; Workable may be rare among Bay Area tech companies.

Dedupe slugs across companies, since "acme" may be generated by several. Negative-cache 404s per (platform, slug) with a TTL of 60–90 days. Re-trigger discovery only when a board 404s during polling or a careers page changes; conditional GETs with ETag/If-Modified-Since make that check cheap.

#### 3. Documented limits and terms

None of the four publish a rate limit for their public read endpoints, so your 2 s floor is a self-imposed courtesy, not a documented allowance.

**Greenhouse.** GET endpoints need no authentication; only application submission requires Basic Auth. The Job Board API page states no limit. A third-party write-up likewise notes no published hard limits, with a warning that aggressive hammering can get blocked. The 50 requests per 10 seconds figures you'll see online refer to the separate Harvest API.

**Lever.** The official postings-api repo documents only one limit: a 429 if a site sends more than 2 application POSTs per second, a limit Lever says may change without warning. Nothing is documented for GETs.

**Ashby.** The official page documents `GET https://api.ashbyhq.com/posting-api/job-board/{JOB_BOARD_NAME}` with an optional `includeCompensation` parameter and says nothing about limits or terms. Omit `includeCompensation` when probing to keep responses smaller.

**Workable.** The help center documents public endpoints at `www.workable.com/api/accounts/{subdomain}?details=true` plus `/locations` and `/departments`, again with no stated limit. Those requests redirect to `apply.workable.com/api/v1/widget/accounts/{slug}`, which returns the same payload. Call the apply host directly so the redirect doesn't hit a second host. One developer reports that an unknown Workable account returns 404 while a real account with no live jobs returns 200 with an empty list, and that Workable rate-limits by IP, returning 429s under even light parallel probing.

All four exist to power careers pages. Polling a known board occasionally fits that purpose well. Thousands of 404 probes look least like normal use, so cutting probes is also the politest change you can make. Two more checks:
- Look at what your robots.txt cache says for the four API hosts. If any disallows the API path, your own rules forbid that endpoint regardless of the docs.
- Send a descriptive User-Agent with a contact address.

#### 4. Architecture issues at ~4,000 companies

**The dispatcher caps throughput.** 250 tasks every 300 s is 50 companies per minute, so 3,800 companies need at least about 75 minutes no matter how fast the workers are. That already breaks your "under an hour" estimate. For backfills, refill whenever queue depth drops below a threshold instead of on a fixed timer.

**Don't wait inside claimed tasks.** Any wait longer than a few seconds (the 60 s company floor, a busy platform host) should become a deferred re-enqueue with a `run_after` timestamp. The claim query then becomes `WHERE run_after <= now() ORDER BY priority, run_after`. With 3,800 distinct hosts, the 60 s floor then barely limits throughput, and a careers-page pass can use 30–50 concurrent coroutines. Key that floor by registrable domain (via tldextract), not hostname, so `jobs.acme.com` and `acme.com` share it.

**Make the Postgres limiter a reservation, not polling.** One atomic statement hands out the next slot. You then sleep without holding a connection:

```sql
INSERT INTO host_slots AS h (host, next_free)
VALUES ($1, now() + make_interval(secs => $2))
ON CONFLICT (host) DO UPDATE
  SET next_free = GREATEST(h.next_free, now()) + make_interval(secs => $2)
RETURNING h.next_free - make_interval(secs => $2) AS my_slot;
```

This spaces request starts. If you mean the floor as a gap after each response completes, reserve on completion instead. A 429 pushes `next_free` to `now() + Retry-After`. If `my_slot` is far in the future, defer the task rather than sleeping on it. With only one process, an in-memory asyncio limiter would be simpler, persisting last-request times only to survive restarts.

**Scoring may block the event loop.** If scoring runs on the event-loop thread, a batch of bge-small encodes stalls heartbeats and lease renewals, causing spurious re-deliveries. Run it via `asyncio.to_thread`, a process pool, or a separate CLI process.

**Watch the DB pool.** 15 connections shared by N workers, the limiter and heartbeats is fine only if no code holds a session across an HTTP await. Check this before raising concurrency, and set `pool_timeout` explicitly so exhaustion fails loudly.

**Tune HTTP.** Split timeouts (connect about 5 s, read about 15 s) instead of a flat 30 s, and stream responses with a size cap of around 2 MB so a few huge pages can't stall the run.

What would help most next is the breakdown of your 167 trial misses: other ATS detected, JS-rendered, no careers page, or dead site. That shows how much each gate would remove before you build anything.

## Findings when working on them (2026-10-04)

Measured before building anything; scratch scripts, nothing committed yet.

- **The sheet's careers column is not careers pages.** 3,797 of 3,802 rows
  are Google search links (`google.com/search?q=site:…+careers+jobs`). The
  importer already rejects them (`is_search_url`), so discovery starts from the
  company homepage and follows one careers link.
- **Name-guessing found 0 of the 188 sheet companies in the trial.** All 28
  sheet boards came from the homepage pass. The 5 guess successes were registry
  companies with no website.
- **Why the 167 trial misses missed** (homepage + one careers link, same
  polite fetcher): no board signal 91, robots.txt unreadable (network/5xx — the
  crawler correctly refuses per RFC 9309) 27, robots.txt disallows 2,
  JavaScript-only page 24, dead site 8, another platform 7 (Workday 2, Breezy,
  Paylocity, iCIMS, BambooHR, Taleo), no website 7, our platform in a form the
  extractor misses 1 (legacy `{name}.workable.com`). So widening the extractor
  (all five answers) would gain ~1, and an other-platform gate ~7.
- **Both directories exist and are usable.** Figshare "ATS Company
  Directory" (Mahesh Bandaru, CC BY 4.0 — attribution required, 9,935 rows,
  published 2026-08-04, Ashby rows stalest) and the Job Board Directory
  (12,122 rows, eight platforms, free, no attribution required, updated
  2026-10-03). Together: 13,734 distinct boards on our four platforms.
- **Directory join, offline:** proposes the exact board for 23 of the trial's
  33 finds (70%), a board for 10 of the 167 misses (some wrong, e.g. Xpansiv →
  ashby `jobs`, Aethos → `aethoshotels`, so identity must be checked), and a
  board for 615 of the 3,552 undiscovered sheet companies (687 candidates).
- **Claims checked against the code:** the shared rate limiter already
  reserves in a short transaction and sleeps after commit (no lock held);
  chunk embedding already runs off the event loop, feed scoring does not
  (~20 s per pass — minor). Grok's "Ashby ~10 s per request" does not hold
  here: measured 91 ms per Ashby request.

### Changes (1–3 built 2026-10-04, 4 not yet run)

1. **Built.** `packages/crawler/board_directory.py` loads both directories
   from `storage/board_directories/` (`make fetch-board-directories`, two
   requests, attribution written beside them; 18,033 boards on our four
   platforms once Workday and the rest are dropped). It proposes candidates by
   domain stem (registrable label, so `jobs.xpansiv.com` → `xpansiv`) first,
   then by exact normalised name, never a generic slug (`jobs`, `careers`, …),
   and at most three.
2. **Built.** `discover_company_job` tries directory candidates first. Each is
   one probe to a shared ATS host, and the board must list at least one
   posting, the bar a guess is held to. Then it reads the homepage as before.
   It name-guesses only when the company has no website:
   `CRAWLER_NAME_GUESSING=no_website`, with `always` restoring the old
   behaviour. A directory hit is recorded as `discovery_from_directory` with
   its reason (`slug matches website` or `same name in directory`), so a wrong
   match can be traced. These are the handler's first tests
   (`tests/test_discover_company_job.py`).
3. **Built.** `make crawl dispatch=1 limit=4000 max_backlog=4000` queues the
   whole sheet in one tick, instead of stalling at the 2,000 backlog default.
   The value rides along to every re-armed tick.
4. **Run 2026-10-05**, below.

### Benchmark: the next 200 sheet companies (2026-10-05)

Same shape as the trial: `make crawl dispatch=1 limit=200 once=1`, 5
workers, the same polite fetcher.

| | trial (homepage + guessing) | with directories |
|---|---|---|
| discovery wall clock | 19 min | **10 min 52 s** |
| boards verified | 33 (28 sheet + 5 registry) | **36**, all sheet companies |
| via directory / homepage / guess | 0 / 28 / 5 | 33 / 3 / 0 |
| end to end, incl. first fetch of every board | — | 15 min |

**The directories produced namesakes, and nothing caught them.** Reading each
board's postings: 5 of the 33 directory boards belonged to a different
company with the same name.

| sheet company | board given | whose board it was |
|---|---|---|
| Artemis (artemispower.com) | ashby/artemis | an AI cyber-defense startup |
| Axle (axlepayments.com) | greenhouse/axle | Axle Informatics, bioscience, Rockville MD |
| Arena (arena.im) | ashby/arena | LMArena, AI model evaluation |
| Armory (armory.io) | ashby/armory | a NYC healthcare-payments startup |
| Beam (beamsolutions.com) | greenhouse/beam | BEAM, a math-education nonprofit |

A false board is worse than a missed one: the company reads as verified, so
it is never looked at again, and the namesake's postings enter the feed
under its name.

`board_directory.confirms` is the fix, reading only what the probe already
downloaded. A name can be shared; a domain cannot. A directory board counts
when one of these holds:

- its slug is the website's `.com` label;
- its postings name the website's domain;
- or, for a match made on the name, the website's label agrees with that
  name.

The third rule distinguishes the two kinds of label:

- **Agree:** `tryascend`, `trybadge`, `arkham` and `better` match the name
  (the last for Better Mortgage).
- **Disagree:** `artemispower`, `axlepayments` and `beamsolutions` each say
  the company's own name is longer than the one that matched.

A slug match was found by the label, so the label cannot vouch for it: that
is how arena.im got LMArena's board.

Replayed over the 33:

| | count |
|---|---|
| right boards kept | 26 |
| namesakes kept | 0 |
| namesakes rejected | 5 |
| right boards turned away | 2 — Azumo (`.co`) and Basetwo AI (`.ai`), coined names whose boards never print their site |

A turned-away board falls through to the homepage pass. It is also kept on
the company's evidence as `unconfirmed`.

The five namesake verifications were reverted in the database the same day.
They were set back to `failed`, with the old evidence under `previous`, and
made due for rediscovery. Their 27 postings were closed, not deleted; none
had a match, a decision or an application.

Two smaller findings:

- **Every miss reads the same way.** `from_url` returns None alike for a page
  naming no board, a dead site and a robots refusal. The reason now says the
  site "did not lead to" a board rather than that it "has no board". Telling
  those three apart needs `from_url` to return its reason.
- **Four boards stored no postings.** Aquabyte, Arkham, Badge and Beam were
  fetched, but every posting was past the 30-day limit
  (`POSTING_MAX_AGE_DAYS`). That is correct, but it means a verified board
  can sit in the registry contributing nothing until it posts again.

### DNS test and robots retry over every miss (2026-10-05)

Both were suggested before building anything else for the misses. The DNS
test was put forward by four of the five answers, and the robots retry was
listed as open after the trial. They covered all 329 companies with a website
that discovery had missed, with the 121 verified companies as a control.

**DNS finds nothing here.** CNAMEs were resolved for `jobs.`, `careers.`,
`talent.`, `apply.`, `hiring.` and `join.` on all 450 domains:

- No subdomain points at Greenhouse, Lever, Ashby or Workable, in the misses
  or in the control.
- So DNS identified none of the 121 boards we already know.
- 56 companies have some careers-subdomain CNAME, and they lead to CDNs and
  hosting: AWS, Cloudflare, Azure, Akamai, Zoho.
- Among the misses, DNS spotted another ATS for 4 companies: Phenom 2,
  Eightfold 1, Breezy 1.

The answers' premise, that careers subdomains CNAME to the ATS, does not hold
for this sheet. The test confirmed the extraction itself works by following
GitHub's and Airbnb's CNAME chains. DNS is not worth adding to discovery.

**The robots retry recovered nothing.** 54 misses had failed at robots.txt
across the two runs: 29 in the trial and 25 in the benchmark. A fresh request
per host, a day later:

| | count |
|---|---|
| domain no longer resolves | 14 |
| TLS certificate failure | 17 |
| timeouts and connection errors | 16 |
| Cloudflare 503/526/530 (origin gone) | 4 |
| readable again | 3 |

All 3 readable sites — AdeptDC, After College and Ace AI — disallow us.
Rediscovering them through the real handler confirmed it: each was refused
at its own homepage. So the robots failures were mostly dead companies, not
transient errors. The certificate failures are not something to work around,
since accepting a broken certificate means weakening TLS.

**That exposed a reason that lied.** All three were recorded as "its own site
did not lead to a supported board", which reads as boardless. They were in
fact refused. `from_url` now returns a `SiteReading` with the board, or why
there is none, and a miss is recorded as one of three things:

- `blocked: … disallowed by robots.txt`
- `its own site could not be fetched (HTTP 503)`
- `its own site names no supported board`

ChatGPT's answer made the same point: a robots denial must not become "this
company has no board".

What this leaves for the remaining misses:

- **Dead sites** are a sixth of all misses and cannot be recovered.
- **JavaScript-only pages** were 24 of the trial's 167 misses. Only rendering
  reads those, which was the one technique all five answers agreed to keep
  out of the default path.
- **Boards no directory lists** remain a question for the Wayback/Common
  Crawl slug index.

### The full run (2026-10-05)

`make crawl dispatch=1 limit=4000 max_backlog=4000`, 10 workers, every
company then due: 3,352 never tried plus 169 earlier misses.

| | |
|---|---|
| companies tried | 3,521 |
| discovery wall clock | 89 minutes, about 40 companies a minute |
| boards verified | **437**: 386 from the directories, 51 from the company's own site |
| by platform | Greenhouse 212, Ashby 149, Lever 62, Workable 14 |
| verified companies overall | 682, from 245 before |
| new postings stored | 4,401; open postings 6,199 to 10,589 |
| postings in the feed | 3,719 |
| task or database-pool errors | 0 |

How the 386 directory boards were confirmed: 248 by the slug being the
website's `.com` name, 89 by the website agreeing with the matched name, 48 by
the board naming the website, and 1 with no website to check. 116 companies
had a directory candidate turned away as unconfirmed; each is on the company's
evidence for a check by hand.

Why the 3,084 misses missed, which the earlier runs could not say:

| | count |
|---|---|
| site read, names no supported board | 2,191 |
| robots.txt unreadable (mostly dead sites, per the retry above) | 626 |
| site unreachable | 189 |
| robots.txt disallows us | 59 |
| board found but lists no open roles | 13 |
| other | 6 |

Three things the run showed about the queue, none of which corrupted
anything:

- **Discovery starves everything else.** The queue claims strictly by
  `run_after`, and 3,521 discovery tasks were queued in one tick, so no board
  was fetched and no matching pass ran for 80 minutes. Nothing was scored or
  embedded until discovery drained.
- **The one-hour provisional claim is shorter than a full-sheet run.** When
  the next tick finally ran it re-queued 9 companies whose discovery task was
  still pending. The strict ordering above is what kept that to 9.
- **A new board is fetched twice.** Discovery queues its first fetch, and the
  next tick queues the routine poll of the same board: 998 fetch tasks for
  582 boards. The second finds nothing changed.

The chunk-embedding backlog (3,346 postings at 500 per five-minute pass) was
cleared with `make chunk-postings` in under three minutes.
