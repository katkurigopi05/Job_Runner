# The retrieval pipeline, as built, against a staged-RAG brief

Written 2026-10-12. The owner supplied a specification for a staged retrieval
and agent system sized for ten million enterprise documents, and asked for it
to be applied here. The scale is an example. This is one person's job search:
10,925 open postings, six résumés, no recruiter mail yet.

This file is the audit that brief asks for first: what each stage is in this
repository today, which of its requirements already hold, which were measured
and declined, and what is left. Numbers are from the owner's database on the
day above unless a date says otherwise. `docs/ML_EVALUATION.md` holds every
run behind them, and nothing here claims more than those runs do.

## What is actually configured

Read from the settings and the database, not from the documentation.

| | |
|---|---|
| Chunk vectors | `BAAI/bge-small-en-v1.5`, 384 dimensions, local, stored as `halfvec(384)` in `posting_chunks` |
| Posting vectors | `lexical-idf@2` revision 1, `vector(384)` on `postings`. This is what the feed scores with, whatever `EMBEDDING_BACKEND` says |
| Vector index | none. Every vector search is an exact scan |
| Corpus | 10,925 open postings, 145,421 chunks of 500 characters overlapping by 25, about 14.3 million tokens. `posting_chunks` is 174 MB |
| Retrieval limits | 5 shown, 100 more listed, keyword pool 30 (120 while the search area is applied), 8 search terms, 200 nearest chunks, RRF `k=60` |
| Second model | off. `CHAT_RERANK_MODEL` is empty; pool 30, widen 50 when one is named |
| Semantic candidates | off (`CHAT_SEMANTIC_CANDIDATES=false`) |
| Relevance grades | 0 rows in `posting_labels` |
| Recruiter mail | 0 rows in `inbound_messages` |

The last two decide most of what follows. With no grades, a change to what is
shown can be counted only by where one known posting lands. With no mail, the
inbox half has never met real input (CLAUDE.md §15).

## The pipeline, stage by stage

| Stage in the brief | Here | Where |
|---|---|---|
| Sources | Board APIs of four ATSes and Workday, JSON-LD on bespoke careers pages, aggregators for discovery only | `packages/crawler/` |
| Parsing | JSON and HTML to text; résumé PDF, DOCX and text; RFC 822 mail | `crawler/extract.py`, `crawler/jsonld.py`, `tailor/parse.py`, `inbox/imap.py` |
| Structure recovery | Requirements headings, skills, experience, pay, eligibility read out of the text | `matching/requirements.py`, `experience.py`, `eligibility.py` |
| Chunking | Fixed 500-character windows as offsets into the stored text, tied to its `content_hash` | `matching/chunks.py` |
| Metadata | Title, company, location, URL, ATS, first seen, closed, requirements, legitimacy | `core/models.py` |
| Filters before search | Open postings, a named company, the search area, a named role | `matching/retrieve.py`, `locality.py`, `roles.py` |
| Dense and sparse together | Keywords decide what is relevant; vectors order it by RRF (§14) | `matching/retrieve.py` |
| Reranking | A shortlist of 30, off as shipped | `matching/rerank.py` |
| Routing | In code, before any model: what is searched, the gap table, the crawl command, the §2.2 refusal | `apps/api/routers/chat.py` |
| Planner and tools | None in the dashboard. The tool loop is the MCP client's, through the HTTP API | `apps/mcp/` |
| Confidence | Coverage stated, "not searched" stated, how a posting asks for a skill, citations checked | `chat.py`, `matching/evidence.py` |
| Human approval | The approval gate; an unanswerable question parks | `apps/worker/apply_job.py`, `core/state.py` |
| Generation | One completion over text the code read first | `packages/llm/` |
| Audit | Every hosted call, every status change | `llm/audit.py`, `application_events` |
| Evaluation | Ranking metrics for the feed; known-target runs for the assistant | `matching/benchmark.py`, `docs/ML_EVALUATION.md` |

## Requirement by requirement

**Holds** means there is code and a test. **Partly** names what is missing.
**Declined** means it was measured or ruled out, with where.

### Ingestion and structure (the brief's phase 2)

| Requirement | Status |
|---|---|
| Type and source recorded | Holds. A posting has its company, ATS, URL and external id; a résumé its version, whether it was tailored, for which posting and by which model |
| Versions and checksums | Holds. `content_hash` covers id, title, location and text; résumés are versioned and never edited in place |
| Changed documents reprocessed, unchanged skipped | Holds. A second crawl emits nothing (Gate 5); a changed posting is re-chunked and never read at old offsets |
| Closed postings leave retrieval | Holds. Every search filters `closed_at`, and postings past 30 days are pruned with their vectors (§16) |
| Failures observable | Holds. Crawl runs, each company's source status, queue retries, `/setup`, `crawl_status` over MCP |
| A parser failure is not an empty document | Holds for résumés (refused at upload). Partly for postings: a Workday posting has no description by construction and scores on its title alone, which the notes record |
| Tika, Unstructured, Docling | Declined. No document here is scanned, multi-column or tabular. Postings arrive as JSON or HTML. The base résumé reads back at a 96% parse score. A dependency is worth adding when a résumé arrives that pypdf cannot read, and not before |

### Chunking and metadata (phase 3)

| Requirement | Status |
|---|---|
| Headings and ancestry on a chunk | Declined for now, measured 2026-10-12. Title and company on each chunk put 3 more of 67 named targets in the five shown, moved no paraphrase, and replaced a fifth of what is shown with postings nobody has judged |
| Sentence and line boundaries | Declined, same run. No gain over fixed windows, at 31% more chunks |
| Tables | Not applicable. Postings carry none worth the name |
| Job metadata | Holds, as columns and as text read at ingestion |
| Résumé claims keep their source | Holds. The fabrication guard scopes a claim to the entry it came from (Gate 3) |
| Exact identifiers stay searchable | Holds. The keyword scan is word-bounded and reads every open posting; a word in no posting returns nothing, 150 of 150 (§20) |
| Parent or neighbouring context | Partly. An excerpt is widened around its match. Returning a parent section was not measured |

### The retrieval funnel (phase 4)

| Requirement | Status |
|---|---|
| Filters before retrieval | Holds for open, company, area, role and freshness, and since 2026-10-13 for sponsorship (a question asking which jobs sponsor is answered from what postings state, `matching/sponsored.py`) and for seniority (a level named in a question is read from titles, `matching/level.py`) |
| Hybrid with a measured fusion | Holds. RRF at a borrowed `k=60` |
| Reranking on a shortlist | Holds, and off: measured live, an embedding reranker bought nothing and a cross-encoder moved 15 named targets up and 1 down (§21) |
| Exact match preserved | Holds |
| One posting once | Holds. A posting is ranked by its best chunk and listed once |
| Citations on every answer | Holds. Labels are parsed out of the answer, and one never supplied is reported |
| Benchmarked before adopted | Holds. `make eval-retrieval` reruns the known-target measurement from a checkout, on the question set in `seeds/` |
| Same model and dimensions unless measured | Holds. bge-small was the weakest of the models ranked on 2026-10-05 and is still what runs: nothing since has shown a gain in what is shown that would pay for embedding the corpus again |

### Routing and control (phase 5)

| Requirement | Status |
|---|---|
| Routing by kind of question | Holds, in code (§22, §27) |
| A bounded planner | Declined for the dashboard. Its assistant has no tools on purpose (§27). Multi-step work is the MCP client's, and each MCP tool is one HTTP call the API authorises |
| Tool inputs, authorisation, audit | Holds. Ids are validated, approval asks the owner, and nothing a model says submits anything |
| Never submits, never invents, never leaks | Holds, each as code: the approval gate, the fabrication guard, mail withheld from a hosted model, résumés reaching the assistant as skill names only |
| Retrieved text is not instruction | Holds as advice in the prompt. As code since 2026-10-12: see the security section |

### Corrective retrieval and confidence (phase 6)

| Requirement | Status |
|---|---|
| Supported, inferred, missing told apart | Partly. A posting says how it asks for a skill; the answer's prose is not checked against it |
| Insufficient retrieval detected | Partly. "Nothing matched" and "not searched" are stated. A weak match has no test: there is no similarity cut-off because there is no graded set to choose one on |
| Retry with a reformulated query | Declined, measured twice. Restating a paraphrase helped on the questions it was tuned on (1 to 6 of 41 in the five) and lost on the held-out set (6 to 5 of 26) |
| Abstain | Holds where the keywords find nothing |
| Evidence shown | Holds. Cited postings first, the rest listed as not cited |
| High-risk actions to a person | Holds |

### Security (phase 7)

Most of this held before the brief and is spread across the suite: the
approval gate, the guard, §2.2 refused before any model, loopback binding,
secrets never shown. What was missing was a set of tests that start from the
attacker's text. `tests/test_red_team.py` is that, and writing it found one
hole.

**The context could be written into.** It is lines, and some are the app's
own (`APPLICATIONS BY STATUS:`, `WAITING ON YOU:`, `[P1] …`). A reply's
subject could carry a line break (RFC 2047 decodes `=0A` to one), and so could
a posting's title, a kind of title, and the question a form is parked on. Each
was written into the context as it came, so one such string added lines under
the app's own headings, in the app's voice. A posting's excerpt could not: it
is cut from whitespace-normalised text. Since 2026-10-12 every string an
employer or a sender wrote is kept to the one line it is given
(`chat._one_line`), and a mail header is one line when it is parsed.

What the tests cannot cover is whether a model *obeys* an instruction it
reads, since no test may reach one. They cover what an obedient model could
then do: start an action (nothing but the owner's own message is matched
against the one command), put a claim on a résumé or in a letter (a stub that
writes what a poisoned posting asks for is refused), or forge the record.

### Evaluation (phase 8)

| Measure | Status |
|---|---|
| NDCG, MAP, MRR, P@K for the feed | Holds (`make bench-matching`), and refuses to name a production candidate on fixture labels |
| Recall and MRR for the assistant | Holds for known targets (`make eval-retrieval`): first, in the five, in the list and MRR, for each set and each form of question, stamped with the corpus size and the settings in force. 67 of the 80 tracked cases could be asked on 2026-10-12; the other 13 are outside the search area |
| Exact-token retrieval | Holds as tests and as the confusion matrix in §20 |
| Citation correctness | Holds per reply. Not aggregated over a question set |
| Groundedness of the prose | Missing, and not buildable from fixtures. It needs a judge and grades |
| Abstention | Holds as tests |
| Latency by stage | Partly. Model reading and writing are timed; search stages are timed only in benchmark scripts |
| Cache hits, ingestion failures, stale postings, approvals | Recorded as rows. Not reported as rates anywhere |

## What is left, in order

1. **Relevance grades, from the owner.** Every declined item above is declined
   for the want of them, and no code produces them. `/label` and
   `next_to_grade` are built. Until there are a few dozen, a better number is
   a better fit to two small question sets.
2. **New questions as the old ones close.** `make eval-retrieval` runs the
   tracked set (done 2026-10-12: it was scripts and files under git-ignored
   `storage/` before). The set shrinks with the corpus: 41 of the 56 dev
   postings and 39 of the 40 held-out ones were still stored when it was
   tracked, and postings are pruned after 30 days. Writing replacements
   takes a model call for each batch, and a rule for which postings to
   write them from that does not favour the ones a search already finds.
3. **Titles that name no level.** Done 2026-10-13 as far as code goes:
   sponsorship and seniority are both read from what a posting says and
   not searched for as words. What is left is the owner's to watch. Nearly
   half the titles in the area name no level, and a question that names one
   leaves them out and says how many (`CHAT_ALLOW_UNKNOWN_SENIORITY=true`
   lists them after). "Mid-level" is the awkward case: 29 titles in the
   area say it, so that question is answered almost entirely by its count
   of titles that say nothing.
4. **Rates from rows already kept**: ingestion failures, stale postings,
   cache hits, approvals against rejections.
5. **The inbox reads one folder.** Its first checkpointed scan reads unread
   mail in `INBOX`; after that it reads every newer UID with `BODY.PEEK[]`, so
   opening a reply on a phone neither hides it nor changes its flags. A filter
   that archives mail out of `INBOX` before the poll can still hide it. This
   remains fixture-tested because no real mailbox is configured.

## What is not going to be built

- A vector database, a document store or a parsing service. Postgres holds
  14 million tokens of chunks in 174 MB and scans them exactly in under a
  second.
- Access-control lists and tenancy. One owner, loopback only (§11).
- A planner or tools inside the dashboard assistant (§27).
- A hosted reranker or embedding service (§3, §2.8).
- Anything adopted on a known-target number alone (§19, §21).
