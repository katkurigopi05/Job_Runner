# Notes on outside material, and what the ranking numbers may claim

Moved here from the root `CLAUDE.md` on 2026-10-11, word for word, so they
load only when work touches this directory. "§N" below is a section of
the root file, which keeps every heading and says where each section went.

## From §15: What the gates do and do not prove

### What six outside repositories were worth

Surveyed on request: `punkpeye/awesome-mcp-servers`, `twentyhq/twenty`,
`nexu-io/open-design`, `Arindam200/awesome-ai-apps`, `sindresorhus/awesome`,
`langflow-ai/langflow`. One produced something to import. Recording the other
five so nobody re-reads them looking for it.

**`punkpeye/awesome-mcp-servers` → one entry, and it is the closest peer this
project has.** `gzchenhao/openhire` (MIT) is an MCP job-search server over the
same ATS APIs — Greenhouse, Lever, Ashby, plus Beisen and Moka for China — with
139 employers against our 105. Three things came out of reading it:

- **76 employer boards we did not have.** Its seed roster carries 114 tenants
  on ATSes we support; 38 were already ours and **none** collided with our
  `retired:` section, so 76 are new. Appended through
  `import_portals.append_to_registry` — the file's single writer — and left
  **unstamped**, because their verification is theirs and not ours. See §9.
- **Its `ghost_score` is a weaker version of what we already have.** A pure
  function of relist count and age past a 45-day grace, which is exactly the
  two inputs `legitimacy._reposting` and `legitimacy._freshness` already feed
  into a tier with six other signals. Adopting it would replace a findings
  table with a number, which `legitimacy.py`'s own docstring argues against:
  a well-written ghost job scores well precisely because it is well written,
  and a single number hides which signal fired. Declined, on the strength of
  a decision this repo already made rather than on taste.
- **Its structural-privacy test does not transfer, and the reason is the
  interesting part.** OpenHire fails its build if anyone adds a résumé field
  to the protocol, because its server is *shared* — the résumé must never
  reach it. Ours is the owner's own machine, and `inspect_resume` returning
  résumé text to the owner's own assistant is the feature. Same words, opposite
  threat model. Copying the test would have broken three tools to satisfy a
  constraint we do not have.

**The other five, and why each is a no:**

- **`twentyhq/twenty`** — an open-source CRM. §11 puts multi-tenancy, accounts
  and billing out of scope, and its pipeline board is the one part that rhymes
  with Phase 6's tracker. Not worth carrying a CRM to get a kanban.
- **`langflow-ai/langflow`** (MIT) — a visual builder for LLM workflows,
  deployed as an API. §11 rules out a hosted runtime, and §7's provider
  abstraction already does the part we need with an audit trail Langflow has no
  equivalent of. A visual graph over five tasks would be scaffolding around a
  60-line router.
- **`nexu-io/open-design`** — an agent-native design tool for prototypes, decks
  and images, fronting a paid model service. Nothing in this project renders a
  deck. Its `DESIGN.md`-as-brand-contract idea is neat and has no application
  to a résumé the fabrication guard governs.
- **`Arindam200/awesome-ai-apps`** — 132 examples; the three nearest are a job
  search agent, a résumé optimizer and a LinkedIn job finder. Every one of them
  needs a paid key (ExaAI, Nebius, Bright Data), which §3 refuses without
  asking, and the job finder is built on Bright Data's scraping network —
  §2.5's proxy-rotation clause, not a borderline case. The résumé optimizer's
  taxonomy is worth one glance and no more: "Career Gap Framing" is a §2.1
  fabrication with a friendly name.
- **`sindresorhus/awesome`** — an index of indexes. Two pointers looked
  relevant, `tramcar/awesome-job-boards` and `lukasz-madon/awesome-remote-job`,
  and both list *aggregator sites* rather than employer ATS boards: zero
  greenhouse/lever/ashby/workable URLs across the whole file. That is discovery
  input, not registry input, and `discover.py` already covers that path.

The pattern worth keeping: of six repositories, the useful one was the only one
solving the *same problem*, and what it contributed was **data** — a list of
employers someone else had verified — not code. The code it has that overlaps
ours is worse than ours, which is the ordinary outcome and the reason to read
before importing.

### What a seventh outside repository was worth

Added 2026-10-12, after the notes above were moved here. The owner forked
`MadsLorentzen/ai-job-search` (MIT) and asked what in it was useful, naming
its layout check. It is a Claude Code framework: prompt files for evaluating,
drafting and tracking, a dozen Python tools (about 3,400 lines) and their
tests. Three things were taken, and each is a *check* it had built after
meeting a failure this project had never looked for.

- **How a robots.txt is read, and this was a breach of §2.6.**
  `tools/robots_check.py` records that Python's `urllib.robotparser` drops a
  group's rules when a blank line follows `User-agent`, and loses the first
  group behind a byte-order mark. `packages/crawler/robots.py` used that
  parser. Run against it: both of those, and three more it did not mention
  (`Allow: /` ahead of a narrower `Disallow`, `*`, and `$`), each read as
  permission for a path the site had disallowed. `robots_rules.py` reads the
  file per RFC 9309 now, and the decisions are in that module's docstring.
- **Where a résumé sits on its pages.** `tools/verify_layout.py` measures a
  compiled LaTeX CV for a thin last page, a hole, and a heading parted from
  its content. Nothing here measured any of it, and the owner's own storage
  held a tailored résumé with two lines on a third page.
  `packages/tailor/layout.py` takes the list of things worth measuring and
  none of the code: that tool needs Poppler and infers a heading from a
  taller line, and this template sets headings smaller than the body.
- **The ignore rules for secrets and résumés.** `tools/security_guards.py`
  holds its template's `.gitignore`. §2.7 and §2.8 rest on ours and nothing
  held it; `tests/test_private_paths_stay_ignored.py` does.

Not taken, so nobody re-reads it looking:

- **The retry with a browser's User-Agent** in `robots_check.py`, for a site
  whose firewall refuses an honest one. That is §2.5.
- **`verify_pdf.py --ascii-dates`.** It fails a CV whose date range is joined
  by an en-dash, on one reported Workday import that dropped such dates.
  LaTeX makes that dash from `--`; WeasyPrint prints what the owner wrote
  (§2.1 reproduces source text), and their résumé does use one. One report
  from another project's applicant, about an ATS this project has no adapter
  for. The owner's to decide; nothing was changed.
- **Its fit score**: four dimensions scored 0 to 100 by the model and
  weighted 0.30, 0.25, 0.15, 0.30, two of them (behavioural, career) read
  from a self-written profile this project does not hold. `rubric.py` is
  computed without a model, and `Match.score` is not to be touched (§16, §19).
- **The drafter and reviewer.** A second model critiques the draft and
  researches the company on the web. Here that is the résumé and posting
  sent to a model a second time (§2.8 permits one upload), to add claims
  about an employer that the fabrication guard cannot trace to the résumé.
- **`job_key.py` and `rank_state.py`.** A dedup key and a ranking pass over a
  JSON file. Ours are constraints and rows.
- **The portal scrapers** (LinkedIn, Jobindex, Jobnet and three more) and
  **the salary lookup**. Danish portals and Danish union statistics, and
  LinkedIn is an aggregator this project reaches boards through, not a
  source (§1).
- **CSV cells that open as formulas.** Its tracker escapes them. This project
  writes two CSVs (`company_csv.py`'s remainder and
  `scripts/export_postings.py`), both read back by programs, where a quote
  put in front of a title would change the data. Recorded and not changed:
  if either is ever opened in a spreadsheet, a title beginning `=` or `@` is
  a formula.
- **Its reviewer's trust boundary** (a posting's text is data, not an
  instruction). `assistant.system` already says so.

Same pattern as the six above, one step on. What was worth having was not a
feature and not data. It was the places where somebody solving the same
problem had been bitten and written the check.

**Its tests, asked of this project.** The owner then asked for the same pass
over its forty test files. Most hold a markdown command file to its own
wording ("the spec IS the implementation"), which has no counterpart here.
What transferred was the *question* a test asks, put to this code, and four of
them found something:

- **Do look-alike hosts fail closed?** (`test_apply_host_check.py`.) One did
  not. Greenhouse's embedded-form pattern was searched for anywhere in a URL,
  so `evil.example/go?u=https://boards.greenhouse.io/embed/job_app?for=acme`
  was a Greenhouse form, both when the adapter was chosen and when
  `navigate._check_still_ours` asked where the browser had landed. The
  owner's profile and résumé would have been typed into it. Anchored now, and
  a form is asked for over https. `tests/test_ats_lookalike_hosts.py`.
- **Is every status in one list, with every reader held to it?**
  (`test_tracker_status_vocab.py`.) `Classification.ASSESSMENT` was in the
  Python enum and not in the dashboard's union, so a tracker card read "read
  as undefined". `tests/test_dashboard_vocabulary.py` holds every union that
  carries an enum's name. The same question of `analytics/` found
  `Outcome.ASSESSMENT` in none of the funnel's or the follow-up list's sets:
  an application with a timed exercise waiting was listed as silent.
- **Is a funnel counted from what happened, or from where things stand?**
  (`test_html_report_command.py`.) From where things stand, and a rejection
  outranks an interview, so the applications that got furthest were the ones
  the engagement rate lost. Engagement is read from the alias-linked messages
  as well now.
- **Does a placeholder survive into what is sent?**
  (`test_placeholder_integrity.py`, about template sentinels there.)
  `rewrite.vet` and `cover.vet` both accepted `[company]`, and the letter's
  greeting and sign-off were never read at all. `packages/tailor/placeholders.py`.

Asked and found nothing: README and docs links (19, all resolve).

Asked and not settled: its `test_scrape_contract.py` holds every portal
reader to one set of output fields, and flags a field that is empty on every
result as a parser that stopped reading it. Here each extractor has tests of
its own in `tests/test_crawler.py` and nothing holds them to one contract
across all six. No defect was found by looking; no test was written either.

**The inbox poll no longer loses mail merely because it was opened elsewhere.**
The first checkpointed scan preserves the old `UNSEEN` footprint. Once that
backlog drains, `inbox/imap.py` requests every UID newer than the committed
high-water mark with `BODY.PEEK[]`, so another client can mark a new reply seen
without hiding it or having this process change its flags. Mail filtered out of
`INBOX` before the poll remains outside this one-folder source. No real mailbox
has been read here (`inbound_messages` held 0 rows at the audit), so the path
remains proven with controlled IMAP fixtures.

### What two outside specs were worth

The owner supplied a job-tracker prompt (MERN/TypeScript LinkedIn scraper) and
an AI-résumé-analyzer prompt (MERN + Gemini). Both are build-from-scratch
specs for other stacks, so nearly all of it is either irrelevant here or
already done better. Three things were not, and this records which — so the
next person does not re-read forty pages to find out.

**An assessment is its own kind of message.** The tracker's pipeline carried
`ONLINE_ASSIGNMENT` as a stage of its own; ours had no equivalent, and the
three commonest phrasings landed badly:

    "Complete your online assessment ... within 5 days"  -> info_request
    "Coding challenge ... Codility link. 72 hours."      -> abstained
    "Take-home exercise ... return within a week"        -> abstained

`info_request` reads as paperwork and an abstention resolves to `noise`, so
either way the window expires while the tracker looks calm. That is a worse
failure than a missed rejection — a rejection is already over, an assessment
is an opportunity with a deadline. `Classification.ASSESSMENT` and
`Outcome.ASSESSMENT` now exist, ranked above `info_requested` and below
`interview`, because an assessment is a real advance that almost always
precedes an interview rather than replacing one.

The rule sits *before* `INTERVIEW` — an assessment invite borrows the same
"next step" vocabulary — and every alternative names the artefact or a
platform rather than a bare "assessment", so "we will assess your
application" is not a coding test. The labeled 30 are unchanged at 29 correct,
0 wrong.

**Three Gemini facts worth having before a key lands**, from the analyzer
spec's list of failures that cost someone a build. They are in
`.env.example`: keys beginning `AQ.` are as valid as `AIzaSy...` so nothing
should format-check them; setting `GOOGLE_API_KEY` alongside `GEMINI_API_KEY`
makes the SDK silently prefer the former; and `GOOGLE_GENAI_USE_VERTEXAI=true`
switches to Vertex, which wants application-default credentials instead.

**A landmine in `GeminiProvider.complete_json`.** The spec's hardest-won
lesson is that Gemini treats every schema property as optional unless named
in `required`, and returns a partial object with no error otherwise. Pydantic's
`model_json_schema()` already emits `required` at every level, so that half is
covered. What it *also* emits, for a nested model, is `$defs`/`$ref` — which
`responseSchema` has not historically resolved. No caller passes a nested
schema today (`complete_json` has no production caller at all), so this is
recorded at the call site rather than fixed: the first nested schema should
check the response before trusting it.

Everything else was skipped deliberately. The tracker's space-padded substring
matching solves a problem `locality.py` already solves with word boundaries;
its match formula is a weighted keyword count where we have embeddings plus a
rubric; and both specs assume a greenfield MERN app.

## §19: hybrid retrieval, and a number that could not be read

`packages/matching/hybrid.py` fuses the dense cosine with BM25 by Reciprocal
Rank Fusion, as two benchmark variants. `docs/ML_EVALUATION.md` carries the
run and the reasoning; this section is the part a later change could break.

It came from surveying the owner's other repositories. `Attorney.AI` — their
own citation-first legal RAG — does hybrid retrieval over pgvector plus
Postgres FTS with RRF at `k=60`, and `dp800-sql-embeddings-vector-search` does
the same thing in T-SQL at the same constant. The technique arrived already
known, which is the only reason it was worth trying before the labeling loop.

**It targets a weakness this file already recorded.** §15: on the twelve
adjacent roles NDCG@5 is 0.577 and the constant control is statistically tied
against everything. A tie with a control returning 0.5 for every posting means
the scorer contributes nothing on the comparisons that decide a real feed. And
adjacent roles are where a dense vector is weakest by construction: a Python
backend role and a Java one differ in a few tokens out of several hundred, the
surrounding sentences are near-identical, and collapsing surface form is what
the embedding is *for* — it is the property `roles.py` exploits to match
"Member of Technical Staff" to "Software Engineer".

**The repo already holds both signals and uses them as alternatives.**
`embed.get_embedder` selects `SentenceTransformerEmbedder` *or* falls back to
`LexicalEmbedder` inside a try/except. Fusion is what turns that into a both.

Five things are load-bearing:

- **It does not touch `Match.score`.** `rubric.py` argues that at length and
  §15 repeats it: the cosine is what `tests/test_matching.py` validates and
  what `Profile.min_match_score` compares against. This is a benchmark
  variant, nothing more, and the feed is unchanged.
- **RRF reads ranks, never scores.** A cosine in [0,1] — occupying a sliver
  of it, since §15 records a real run peaking at 0.271 — and an unbounded
  BM25 have no common scale, and weighting them means fitting a weight on the
  same labels the result is reported against. That is `REFERENCE.md` §3.6's
  self-evaluating referee. `Ranking` stores ranks and nothing else, so the
  guarantee is structural rather than a rule someone has to remember.
- **`k=60` is borrowed, not tuned.** From the original paper and from both of
  the owner's own implementations. A constant taken from elsewhere is
  evidence; one fitted to twelve labeled postings is an overfit wearing the
  same digits.
- **A document one side never returned is not ranked last.** A vector search
  with a `TOP_N` returns a cutoff, not an ordering, and scoring the absent
  document as worst lets that cutoff veto the other signal's first place.
- **The fusion variants are absent without a corpus, not degraded.** There is
  no such thing as the rank of one posting on its own, so `default_variants`
  omits them when `items` is not supplied rather than scoring something else.

### The result, and why the result is not the point

```text
production   0.577      hybrid_rrf   0.452      bm25_only   0.452
                        rho(dense, lexical) = +0.916
```

The fusion lost, and **that row is not a verdict on hybrid retrieval.**
`sentence-transformers` is absent here, so the "dense" side fell back to
`LexicalEmbedder`, a hashed bag of words, and the fusion was BM25 against a
differently-weighted view of BM25. `hybrid_rrf` landing level with
`bm25_only` to three decimals is the ablation firing exactly as intended.

**A number that cannot be interpreted is worse than no number**, because it
closes a question it never opened. Reported bare, 0.452 reads as "tried it,
it lost". So `hybrid.agreement` is computed on every fusion pass, recorded in
the experiment record so it survives into `--json`, and printed under the
table with the refusal spelled out in words. `DEGENERATE_AGREEMENT` is 0.85
and deliberately loose: RRF is worth running on signals that mostly agree,
since the wins come from the minority they order differently. What the
threshold catches is a signal fused with itself.

This paragraph used to end "finishing it needs the owner's machine", because
bge-small downloads from `huggingface.co` and the cloud session was denied at
CONNECT. It was finished there on **2026-09-30**:

```text
--tag adjacent --k 5 --real-embedder         rho(dense, lexical) = +0.573
production   0.405      hybrid_rrf   0.358      bm25_only   0.452
```

**At +0.573 the row is a measurement, and fusion lost.** The fused ranking
came in below both of its inputs. It carries the same two false positives as
`production` (`adj-junior-backend`, `adj-eng-manager`), and BM25 alone had kept
the manager out. Every row except the control is still inside every other
row's interval on twelve fixture labels, so the claim is only that fusion did
not help here. It stays a benchmark variant. Full table in
`docs/ML_EVALUATION.md`.

Two corrections came out of the run, and both are the kind that stay invisible
until the real embedder is loaded:

- **The 0.577 in §15 is the lexical fallback's number.** `bench_matching`
  uses `LexicalEmbedder` unless `--real-embedder` is passed, whatever is
  installed. So "sentence-transformers is absent here", above, was only half
  the reason the first run was lexical: the default would have been lexical on
  any machine. On bge-small `production` is 0.405 on the adjacent slice.

  This bullet first called bge-small the embedder the owner's feed runs,
  because `.env` names it. It is not: once corpus statistics exist, the
  matching pass scores with `lexical-idf@2` whatever `EMBEDDING_BACKEND` says,
  and the benchmark runs neither that embedder nor its weights. The feed's own
  number on the adjacent slice is unmeasured; `docs/ML_EVALUATION.md` has it.
- **The degenerate check had one remedy for two causes.** With bge-small
  loaded, agreement on the Gate 5 slice is +0.875, because off-domain
  negatives rank last under any scorer. The printed advice was still "install
  the `embeddings` extra", which was already installed. `hybrid.agreement_note`
  separates them. The lexical fallback is told to install the extra. A real
  embedder is told the slice cannot discriminate and pointed at
  `--tag adjacent`. "A signal fused with itself" was only one of the two
  things the threshold catches.

**No cross-encoder reranker, deliberately.** `ms-marco-MiniLM-L-12-v2` is free
and local and jointly encodes (query, passage) — the comparison a cosine is
worst at. It needs `torch`, nothing here can fetch the weights, and an
unmeasured ranking stage is the shape this file keeps recording as a defect.
Recorded as a candidate with its cost attached rather than shipped dark.

**BM25 is in the tree rather than a dependency**, for two reasons that are
both about the measurement transferring. It scores through `embed.tokenize`,
so the experiment measures fusion rather than two tokenizers disagreeing about
`node.js`. And it is what a production path would run: Postgres FTS is the
obvious source and `ts_rank_cd` is a different ranking function, so a
benchmark won on BM25 would not transfer to a feed served by FTS.
`apps/api/routers/matches.py` already filters in Python for the reason it
always has — the fields are read out of text.

One incidental find, from a test rather than from reading. The IDF carried a
`max(0.0, ...)` clamp with a comment explaining which case it protected
against. There is no such case: `n <= N` makes the quotient non-negative and
Lucene's `+ 1.0` inside the log puts the argument at or above 1, so the
result is always non-negative — checked exhaustively for every `(N, n)` to
200, minimum 0.0025. The clamp is gone and the comment now names the term
that actually provides the guarantee. A guard that cannot fire reads as
protection against a risk that does not exist, which is the same failure as a
test that cannot fail.
