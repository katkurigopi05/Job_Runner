---
paths:
  - "packages/matching/retrieve.py"
  - "packages/matching/rerank.py"
  - "packages/matching/roles.py"
  - "packages/matching/gaps.py"
  - "packages/matching/evidence.py"
  - "packages/matching/chunks.py"
  - "apps/api/routers/chat*.py"
  - "tests/test_chat*.py"
  - "tests/test_retriev*.py"
---
# Notes on the assistant's search

Moved here from the root `CLAUDE.md` on 2026-10-11, word for word, so they
load only when work touches the files named above. "§N" below is a section of
the root file, which keeps every heading and says where each section went.

## From §14: The assistant

### Retrieval: answering questions about the postings themselves

The context above was fixed — counts, profiles, one application, three
replies — so "which open roles use Kafka?" had nothing to be answered from.
`packages/matching/retrieve.py` finds the postings a question is about and
the chat route puts them in the context as `[P1]`…`[P5]`, with an excerpt,
the company, and the owner's application status if there is one. The model
is told to cite them; the reply's `sources` marks which it did, read out of
the text rather than taken on the model's word, and the dashboard lists the
cited ones first. It used to list only those; §20 records why it no longer
does.

Four things are load-bearing, and the first two were each found by running
it against the owner's database rather than by reading it:

- **It only searches when the question is about postings** — a job word, or
  a registry company named in full. The first version always searched, and
  "what needs me?" came back with five HelloFresh customer-care roles from
  Manila. Corpus rarity cannot make this call: "me", "waiting" and "replies"
  are rare in job postings precisely because postings do not talk that way.
  A question that is not searched says so in the context
  (`POSTINGS: not searched`), for the same reason withheld mail does.
- **Keywords decide relevance; embeddings only decide order.** A pure
  vector search for the Kafka question returned one posting mentioning Kafka
  and four sharing "open" and "roles". Now a posting must contain one of the
  question's distinguishing terms — IDF-weighted, word-bounded so "rust" is
  not "trust" — and vectors re-order those matches by RRF. The keyword scan
  reads every open posting, including the 6,833 of 19,018 with no usable
  vector, which no vector search can see; in the fusion such a posting takes
  the middle vector rank rather than none, or the best keyword match could
  drop out of the top five for lacking an embedding. (The commit that added
  this called all 6,833 unembedded. 6,769 are; the other 64 carry a vector
  with no `embedding_model` stamp, so no question can be encoded into their
  space and they are skipped the same way.)

  Framing words are never search terms, and rarity cannot be trusted to
  remove them: "me" is in 1.9% of postings, and "Show me forward deployed
  engineer jobs." came back as five Pleo postings saying "Show me the
  benefits!". So the question's framing ("which open roles") and the asker
  ("show me", "find", "I'm looking for") are listed explicitly.

  Vectors answer alone only when the question has *no* distinguishing term —
  "any data engineering roles?", where both words are in most postings. Never
  when the terms matched nothing: bge-small's similarity is almost never zero,
  so "roles using Zig?" would otherwise get the five nearest postings whether
  or not any says Zig.
- **One vector search per embedding space, never across.** The owner's
  database held 10,185 bge-small vectors and 2,000 `lexical-idf@2` ones side
  by side. The question is encoded once per space, each space is searched
  only against its own rows, and a space nothing can encode into — a retired
  model, idf weights from a superseded revision — is counted as unsearchable
  rather than approximated.
- **Coverage is part of the answer.** "None found" over every open posting
  and over a vector-reachable sixth of them are different answers, so the
  context states how many were searched and the model is told to repeat it.

**§2.8 is unchanged.** The question is embedded locally, and nothing in
retrieval calls a provider. What it adds to the context is employers' public
posting text plus the owner's own application statuses, which go wherever
the rest of the context goes under the per-question provider choice above.
Recruiter mail gating is untouched.

**Chunks, not one vector per posting** (2026-10-01). A posting's single
vector covers what bge-small reads, its first 512 tokens, and postings run to
a median 5,874 characters with the requirements last. Of the 143 postings
that mentioned RAG, the mention fell inside the embedded window in 27, and a
semantic search for "RAG" found none of them in its top 50. The fix is the
owner's own DP-800 recipe (`AI_GENERATE_CHUNKS`, fixed 500-character chunks
overlapping by 25): `matching/chunks.py` splits each open posting,
`posting_chunks` stores one `halfvec(384)` per chunk, and search ranks a
posting by its best chunk. Measured before building, chunking took "RAG" from
0/10 to 4/10 in the semantic top ten; float16 halved the vectors (124 to 62
MB) at the same top-10 precision.

Four things are load-bearing:

- **Keywords still decide relevance.** Chunk ranks feed the same RRF, last,
  so they override a posting's one-vector rank, never the keyword pool. The
  DP-800 hybrid (top 15 from each side, unioned) put "Supervisor Sanitation"
  second for "RAG": bge-small reads it as the cloth.
- **Offsets, not text.** A chunk is `(start, length)` into the normalised
  description, plus the `content_hash` it was cut from. A chunk whose hash no
  longer matches its posting is ignored by search and replaced by the next
  pass, never read at the wrong place.
- **Only bge-small chunks.** Lexical chunk vectors would repeat what the
  keyword scan reads, so with `EMBEDDING_BACKEND=lexical` the pass does
  nothing and search behaves exactly as before. Chunk queries carry bge's
  documented search prefix, which the measurement used.
- **384 dimensions, not DP-800's 1536.** That is Gemini's embedding size;
  using it here would send every posting chunk and every chat question to
  Google, ending the property that retrieval runs on this machine.

The matching pass after each crawl chunks up to 500 new postings;
`make chunk-postings` clears a backlog.

**Questions that name a company or a title** (2026-10-01). Measured on 18
questions built from the owner's data, 10 found what they named. Now 17;
the eighteenth names a company with nothing open, and the context says
"no open postings at Mistral AI" rather than leaving the model to read
silence as no match. Four rules:

- **A bare title is a postings question** when every word of it is in some
  open posting's title. That keeps "did the hiring manager reply?" out, as
  does treating "hiring manager" as a person rather than the job word
  "hiring".
- **A company has aliases**: the bracketed name on its own ("CoreWeave"),
  the name without it ("Weights & Biases"), and either without a suffix like
  AI or Labs when what remains is in at most 2% of postings. "Mistral" names
  Mistral AI; "together" does not name Together AI.
- **A title match counts twice** in the keyword score.
- **A posting whose whole title is in the question goes first**, longest
  title first, two words at least, found by its own lookup so it never loses
  its place in the 30-posting keyword pool. "Director, IT Operations" needed
  it: "it" is a stopword and dozens of Director titles tied ahead.

**The search area applies here too** (2026-10-02). The feed kept to the
owner's area — its top 200 were all in the Bay Area — while the chat search
read a posting's location only to print it: 11 of 25 answers to five
ordinary questions were in London, Tokyo, Singapore, Toronto and Stockholm.
Now 0 of 25. Three rules:

- **The feed's rule, not a copy.** `locality.area_exclusion` (first written
  in `search.py`) is the one statement of the area; the scoring gate, the
  feed and `retrieve` all call it, so they cannot drift. `SEARCH_US_ONLY=false`
  turns it off for the feed and the chat. See §15, *Three rules for one area*.
- **Filtered before the pool is cut.** With the area on, the keyword scan
  reads 120 matches rather than 30 and the area cuts them back, so a foreign
  posting never takes a place in the re-ranking an in-area one could have
  had. The vector-only path widens the same way.
- **Waived when the question names a place outside it**, read by
  `locality_of`: "jobs in London" is a question about London. And what the
  area left out is stated in the context, so "jobs at Faculty" gets "no open
  postings at Faculty in the search area; 20 left out" rather than reading
  as a company that is not hiring.

Measured live on 2026-09-30, llama3.1 answered the Kafka question citing five
postings that all mention Kafka. A search costs 0.1–0.9s against 19,018
postings; the first call in a process also loads bge-small, about 4s, in a
worker thread so the event loop is not held. The keyword scan is two passes —
one combined regex, then per-term scoring over a materialised CTE of the
matches — because one regex per term over every row took 3.1s for eight
terms against 0.8s this way. There is no vector index on
`description_embedding`, so the vector half is an exact scan — if an ivfflat
index is added, the per-space filter makes it approximate.

## §20: what asking the assistant real questions found

On 2026-10-03 the assistant was asked real questions against the owner's
database, first with llama3.1 and then with Nemotron on OpenRouter, and every
cited posting was checked against its stored text. Search was mostly right.
What reached the model was not, and the dock hid what the model then missed.
Six defects, each with a test.

**The excerpt hid the evidence, and this was most of it.** Both models "under-
cited" — llama3.1 cited 2 of 5 Kafka postings and answered "no roles use Zig"
over the one posting that does. The models were answering correctly from what
they were handed: `excerpt()` showed the asked-about word in **88 of 140**
retrieved postings that contain it (30 technology questions). Two causes:

- It scored windows on the question's own words, including ones the search
  drops for being in most postings. "use" is in 57%, so "easy to use" tied
  with the window naming Kafka, and the earlier one won. It now scores on the
  terms the search used.
- Windows started only at sentence boundaries. A requirements list without
  full stops left Starburst's "Production RAG pipeline" 1,247 characters from
  the nearest start. Windows may now also start a little before each match.

After: **140 of 140**, and no posting where the old excerpt had the word and
the new one lost it. On the same question Nemotron went from citing 3 of 5 to
5 of 5.

**Asking verbs were searched for.** "mention" is in 1 open posting and "RAG"
in 134, so "Which roles mention RAG?" ranked "be sure to mention that bonfires
are your jam" first. The §14 framing list now carries the asking verbs —
mention, say, talk about, refer, contain, involve, ask — each measured rarer in
postings than a typical subject.

**The dock listed cited postings only.** §14's reasoning, that listing the rest
would present context as evidence, holds for what sits under the answer, so
cited postings still do. The rest follow under "not cited in the answer". With
the old rule, a model that overlooks or denies a posting leaves nothing on
screen to contradict it.

**Citations the parser could not see.** Nemotron writes `【P1】` and opens list
lines with `- P1:`. Both now count; a bare `P1` mid-sentence still does not.

**"What needs me?" missed the forms waiting to be finished by hand.** The
context carried counts by status, and `failed: 4` cannot say that three were
`manual_completion_required`. A `WAITING ON YOU` section now uses
`notify.needs_owner`, the doorbell's own definition, so the two cannot drift.

**Green-card holders were read as excluded.** "U.S. citizenship or permanent
residency" and "U.S. citizenship or U.S. permanent resident" both read as
citizens-only: "residency" is not "resident" plus a suffix, and the second
"U.S." stopped the gap at its full stop. Letting the gap cross any full stop
was tried first and swept CoreWeave's export-control clause, on 103 postings,
into a residency rule — its options (B) and (C) admit people who are not US
persons. The gap now crosses only a "U.S." in front of the noun, and that
clause is pinned as unstated. Exactly 3 open postings changed verdict. The 48
"U.S. Person" export clauses already read as citizens-or-residents before this
change were not revisited; refugees and asylees qualify under that definition
too.

### Text, semantic and hybrid retrieval, measured

The shipped hybrid lets keywords decide relevance and vectors only order the
matches (§14). That cannot reach a posting worded differently from the
question, so it was measured against the alternatives, offline, on the live
corpus:

| | known item, natural (MRR) | known item, paraphrased (MRR / Hit@20) | judged P@5 |
|---|---|---|---|
| text only | 0.645 | 0.106 / 25% | **90%** |
| semantic (bge-small chunks) | 0.581 | **0.335** / 50% | 65% |
| hybrid, shipped | **0.756** | 0.147 / 31% | 82% |
| hybrid, RRF union | 0.632 | 0.291 / **56%** | 80% |

Known item: 16 random in-area postings, a question per posting written by
Nemotron in two forms, the second forbidden from reusing the title or any
technology or product name. Judged: 8 open questions, the top 5 of every mode
pooled, shuffled and graded 0–2 by Nemotron from two windows of each posting.

Read it as: the shipped design is right when the question names its subject,
and blind when it does not; semantic search reaches paraphrases and pays for it
in near-topic noise ("fraud detection for payments" returned a Security
Incident Responder). **Nothing was changed on the strength of this**: 16 and 8
questions, one free model as writer and judge, and the "natural" questions
nearly all named a company, which only the shipped path filters on. It is the
evidence for a decision, not the decision.

### The assistant reads the owner's résumés, as skills

"What am I lagging on?" is now answered from a count. `matching/gaps.py` reads
the skills on every **base** résumé with `skill_vocab` — the same vocabulary
`requirements.py` reads postings with at ingestion — and counts the skills the
owner's top 100 open matches require or prefer that no résumé lists. On the
owner's database on 2026-10-03: Go required by 12 of 100, Kubernetes by 8,
then Elasticsearch, GCP, Terraform, Azure, Kafka and Linux; Python (20), machine
learning (13), LLMs (12) and SQL (10) already on the default résumé. 44 ms.

Four rules carry the weight:

- **Only demands count.** A skill named under no requirements heading is
  `unclassified`, and `requirements.py` records that it is not a demand.
- **One count per posting, at its strongest.** Required and preferred in one
  posting is one required.
- **Tailored résumés are not read, and skipped matches are not targets.** One
  was bent toward a single posting; the other the owner already declined.
- **Only skill names and counts reach the model.** The chat context can go to a
  remote provider, and §2.8 permits one résumé upload, tailoring. A skill list
  is the smallest thing that answers the question; a test asserts a distinctive
  résumé sentence never appears in the prompt. The provider warnings in the
  picker now say the skill list is sent.

The section is added only to questions about gaps, skills or the résumé. The
prompt (`assistant.system` v3) tells the model that a skill missing from the
résumé is what the résumé does not show, not proof the owner lacks it, so it
must not suggest adding one unless the owner says it is true — §2.1 applied to
advice.

One limit, seen live: for "What am I missing for Kafka jobs?" each retrieved
posting carries what it asks for and what no résumé lists, and Nemotron
answered from the overall table instead, citing none of them.

### Words in the column and words not in it

A confusion matrix over 300 words in 5 rounds: half sampled from in-area
postings' own text (rare, mid and common), half dictionary words in no open
posting. Top 5 of each mode, against the in-area postings containing the word:

| | recall | specificity | precision@5 | recall, rare words |
|---|---|---|---|---|
| text | 1.00 | 1.00 | 1.00 | 1.00 |
| semantic | 0.41 ± 0.08 | **0.00** | 0.19 ± 0.04 | 0.22 |
| hybrid, shipped | 1.00 | 1.00 | 1.00 | 1.00 |
| hybrid, RRF union | 1.00 | **0.00** | 0.62 ± 0.02 | 1.00 |

The keyword modes score 1.00 partly by construction, since the truth here is
the word's presence, which is what they match on. What the test does establish:
the shipped search never invents results for a word in no posting, because §14
does not let vectors answer when the terms matched nothing; semantic search
cannot say "none" — all 150 absent words got five results — and finds a rare
word one time in five. A union hybrid inherits that. So the paraphrase gain
measured above can only be had where keywords matched something; pooling must
never fill an empty keyword result.

## §21: a second model re-orders the assistant's search

`packages/matching/rerank.py`. The assistant's chunks are embedded with
bge-small because it is fast, and it was the weakest of four models measured
on 2026-10-05. Qwen3-Embedding-0.6B re-scoring the best chunk of the top 30
results took the right posting into the top five for 77% of test questions
against 64%, nearly what re-embedding the whole corpus with it gets (80%),
with nothing to backfill. `docs/ML_EVALUATION.md` has the tables.

**Those numbers were a simulation, and the live search did not repeat them.**
It is switched off on the owner's machine as of 2026-10-05. The 77% came from
re-scoring the top 30 of a pure bge-small vector search over a 1,000-posting
pool. The shipped search is not that: keywords choose the candidates and
order them, with title matches counted twice (§14), and the re-ranker replaces
that order. Run through the real `retrieve()` over all 10,589 open postings,
same 56 postings and 112 questions:

| in the five shown | re-ranker off | re-ranker on |
|---|---|---|
| question names the job | 70% | 64% |
| question paraphrases it | 7% | 7% |
| median time per question | 0.5 s / 0.8 s | 2.1 s / 2.4 s |

On the questions that name the job it moved the right posting down on 11 and
up on 4, some of them a long way (rank 2 to 21). On the paraphrased ones the
right posting was never in the list for 47 of 56, and a stage that only
re-orders cannot fix that. So it cost 1.6 s a question and bought nothing
measurable. Three questions separate 70% from 64%, so "worse" is not
established; "no better" is. The code stays, off by default as it always was.
What the paraphrased row needs is candidates the keywords did not find, which
is a change to §14's rule and not a model.

Five properties a later change could quietly break:

- **It only re-orders.** It is handed what the search found and returns the
  same postings in a new order. §14's rule stands: keywords decide what is
  relevant. `test_it_never_adds_a_posting_the_search_did_not_find` holds it.
- **Off unless a model is named.** `CHAT_RERANK_MODEL` ships empty, so a fresh
  checkout neither downloads 1.2 GB nor holds 0.7 GB in the API process. The
  owner's `.env` named `Qwen/Qwen3-Embedding-0.6B` until the live measurement
  above, and is empty again. `conftest._no_reranker`
  blanks it for the suite, for the reason `_lexical_embedder` exists: a suite
  whose verdict depends on the developer's `.env` is not testing the code.
- **A failure leaves the search's order.** No model, a load error, an encode
  error: the assistant answers from the first-stage order and the reply's
  `postings_reranked_by` is null. It never falls back to another model.
- **A posting asked for by its title stays first**, and a question that only
  names a company is not re-ranked. "jobs at Astranis" says nothing to order
  by: on the live database it shuffled one company's postings by their
  likeness to its name and took 1.5 s to do it.
- **It stays on this machine.** The model is local, like the rest of
  retrieval, so §2.8 is unchanged and nothing new leaves.

It adds about 1.3-1.8 s to a question (30 chunks at 19 a second on the M4's
GPU) and holds two models in the API process, bge-small and the re-ranker:
free memory went from 58% to 42% with both loaded. The pool is
`CHAT_RERANK_POOL`, 30; 100 measured no better.

**The feed is untouched.** `Match.score` is still the cosine `rubric.py` and
§19 defend. §19's "no cross-encoder reranker" was about the feed and about an
*unmeasured* stage; this one is the assistant's, and measured first. Measured
in simulation, that is, which is the lesson of this section: §19 asked for a
measurement before a ranking stage ships, and a measurement of something other
than the shipped path did not count as one.

### A cross-encoder, and the one place the keyword rule gives

Added 2026-10-08, after a second forum reply suggested re-ranking bge-small's
top 50 with a cross-encoder. `CHAT_RERANK_MODEL` may now name one: a model
that reads the question and one passage together and scores whether the
passage answers it. The model named says which kind it is
(`rerank._is_cross_encoder`), so there is no second setting to disagree with
the first. Off as shipped, and off on the owner's machine until they say.

Measured in three steps, each on the 49 of the 56 benchmark postings still
open. Natural questions, right posting in the five shown:

| | in the five | first |
|---|---|---|
| bge-small's vector search alone, its top 5 / its top 50 | 23 / 37 | |
| simulation: the search's list and the vector top 50, judged and fused | 45 | 30 |
| the real `retrieve()`, no re-ranker | 37 | 22 |
| the real `retrieve()`, the cross-encoder only re-ordering | 38 | 26 |
| the real `retrieve()`, with candidates added | 40 | 26 |

Live, the right posting ranked higher on 15 questions and lower on 1 (MRR
+0.089 [+0.043, +0.145]). The Qwen re-ranker above moved it down on 11 and up
on 4. Questions that paraphrase a posting did not move: 2 of 49 to 3.
`cross-encoder/ms-marco-MiniLM-L-12-v2` (33M parameters) and
`BAAI/bge-reranker-base` (278M) gave the same counts in simulation, so the
small one is what was run live. `docs/ML_EVALUATION.md` has the tables.

A model of this kind is used differently from the one above, in three ways:

- **It reads the title, company and place over the chunk.** With the chunk
  alone, which is what the Qwen re-ranker read, it made the same questions
  worse than no re-ranker: 24 to 28 of 49 in simulation. A chunk is 500
  characters from the middle of a posting and almost never says what the job
  is called.
- **Its order is fused with the search's**, not put in its place, and its
  rank is among everything it judged. The first build took the candidates it
  rejected out before ranking. That was not what had been measured, and it
  was one question of the 40.
- **It is handed up to 50 postings the keywords did not find**
  (`CHAT_RERANK_WIDEN`), the nearest by bge-small, and keeps one only when it
  scores it above zero. That is its own line for "this answers the
  question". A likeness has no such line, so the first kind is handed none.

The third loosens §14, where keywords decide what is relevant, and §20 is why
it is held narrowly. `tests/test_chat_cross_encoder.py` holds each of these;
every one was broken in turn and caught, nine of nine:

- **Added to what the keywords found, never in its place.** A question about
  a word in no posting is still answered with nothing and the model is not
  asked: 50 of 50 live with it on. For 40 words a few postings do contain,
  126 postings were shown and one did not say the word.
- **Never to a role's own list** (§22), **past the search area, or past a
  named company.**
- **A model that fails adds nothing.** An unjudged candidate is the
  near-topic posting the rule exists to keep out.
- **`CHAT_RERANK_WIDEN=0`** is the cross-encoder with §14 as it was. Most of
  the gain is there: first place is 26 either way.

Why 45 became 40: the simulation re-ordered everything, and the search puts a
posting asked for by its title first (§14). In four questions five or more
postings go first for their title alone and the one the question was written
from is not among them. In two of those the model put it first of the rest.
That rule is the owner's and was left alone.

Not measured: the time on the GPU inside the API process. The live run was on
the CPU. With the API and dashboard running the machine starts near 62% free,
and a second process using the GPU went under the 55% floor the run was held
to. There it added a median 1.2 to 1.6 s a question and held 0.97 GB; only
re-ordering added 0.6 s. Offline on the GPU the model scored 246 pairs a
second, and a question is about 80 pairs.

Seen and not fixed: "…bare metal for AI-native firms?" is read as naming the
company Native AI, which has nothing open, so that question is answered with
nothing whatever the re-ranker.

## §22: a question that names a kind of job

The owner asked the assistant "any jobs with job types AI engineer" on
2026-10-05. It listed three postings titled exactly "AI Engineer", a director
of programme management, and said no requirements were listed. In the search
area 61 postings were that role. Four causes, each with a test in
`tests/test_chat_role_questions.py`:

- **"types" was the search term.** "AI" is in 81% of postings and "engineer"
  in 39%, so §14's rarity rule dropped both and searched on the one word left.
  "type", "types", "kind" and "kinds" are framing words now.
- **Only a whole title in the question was found by title.** A named role is
  now looked up through `matching/roles.py`, the table the feed's role filter
  uses, so "AI engineer" finds "Staff AI Engineer" and "Machine Learning
  Engineer" and not a Product Manager posting that mentions one. The words
  that named the role are not searched for; what is left of the question
  ("using Kafka") is searched among that role's postings, and if none says it
  the role alone is the answer.
- **Nothing said there were more.** `Retrieval.role_total` is how many open
  postings in the area carry the title. The context tells the model the
  number; the reply carries the next matches as `more_sources` (up to
  `MORE_LIMIT`, 100), and the dock lists them ten at a time, with a link to
  `/matches?role=…` for whatever is past that.
- **The excerpt was the employer's pitch.** Found by title, a posting has no
  term to quote on, so `excerpt` took the first 600 characters. With nothing
  to quote it now starts at the requirements heading, when
  `experience.heading_kind` finds one (1,057 of 1,500 sampled postings).

Three things to keep:

- **Five is what fits in a prompt, not the answer.** `more_sources` exists for
  every search, not only role questions. They were never shown to the model,
  so they carry no citation label and are never marked cited.
- **A posting the area leaves out is counted once**, by id. One posting can be
  found by its role and by its exact title; the first version counted it
  twice, and two of the area tests caught it.
- **Roles outside the table are not helped.** "Product manager jobs" is still
  a keyword search. The table is nine engineering and data roles, and it also
  feeds scoring and Gate 5, so extending it is a change of its own.

Measured on the live database: the owner's question now returns role
"machine learning engineer", 61 in the area, five shown and 56 listed, in
about 1.9 s with the re-ranker on. Role matching reads every open title and
costs 0.1-0.2 s, including the 1,027 software-engineer postings.

### One role, several kinds of title

The owner asked for the heading to read "AI / ML engineer", "if the
description is the same". It is not, measured on the in-area postings on
2026-10-06, 18 titled AI engineer against 39 titled machine learning engineer:

| names the skill | AI-titled | ML-titled |
|---|---|---|
| LLMs | 50% | 33% |
| machine learning | 39% | 97% |
| PyTorch | 17% | 69% |
| TensorFlow | rare | 41% |
| TypeScript | 17% | rare |

Their skill profiles are 0.80 alike. Machine learning engineer and data
scientist score the same 0.80, and the table keeps those two apart. Small
samples, so a direction rather than a measure: here "AI engineer" mostly means
building with LLMs and "ML engineer" mostly means training models.

So the role is *named* "AI / ML engineer" (`roles.display_name`, also the
feed's dropdown) and *listed* the way the owner then suggested, "like a
dictionary":

- **Each kind of title is a heading with its postings under it.** A kind is
  the alias a title was recognised by (`roles.kind_of`): on the corpus, AI
  engineer 18, machine learning engineer 41, deep learning engineer 1, applied
  ML engineer 1. Nothing is invented; a title worded "Agentic AI Engineer"
  would be listed under AI engineer the day one is crawled.
- **The kind that was typed goes first**, then the others by size. Asked for
  "AI engineer", the eighteen are not behind forty-one that happen to be newer.
  The candidates are ordered by kind before the re-ranker, so its thirty
  places go to what was typed, and again after it, so it orders within a kind
  and not across.
- **Counts are of the role, not of the page.** `Retrieval.kinds` is counted
  over every in-area posting of the role; the reply's `matched_kinds` and the
  context line carry it, so the model can say "18 AI engineer and 41 machine
  learning engineer" while being shown five.
- **Related roles are links, never results.** `roles.related_to` names
  neighbours (data scientist and data engineer for this one) and the dock
  links them to the feed. Their postings are not mixed in, and the alias
  table, which also feeds scoring and Gate 5, is untouched.

Not done, and the measurement is the argument for it: splitting the row into
two roles. That changes the feed's filter, the scorer's title match and the
Gate 5 set, so it is a change of its own.

### A word too common for descriptions, looked for in titles

The owner asked "any roles with AI" on 2026-10-06 and was shown a data-centre
hardware engineer, an internal-communications role and an account associate,
all at OpenAI. "AI" is in 81% of descriptions, so §14's rule dropped it as a
term; with no term left the search fell back to vectors, and the nearest
things to the two letters "AI" are employers with AI in their name.

The same words are rare where a posting names itself:

| | descriptions | titles |
|---|---|---|
| AI | 81.0% | 4.5% |
| data | 75.7% | 3.8% |
| security | 35.5% | 3.0% |
| product | 69.3% | 7.2% |

So when nothing in a question is rare enough to search descriptions for, and
no role from the table was named, its words are looked for in titles
(`retrieve._title_word_hits`), and the result is the same dictionary: 222
postings for "AI" in the owner's area, AI engineer 18 first, then applied AI,
AI security engineer, applied AI architect, then kinds that carry the word in
a qualifier (software engineer 32, for "Software Engineer, AI Infrastructure").

Five things to keep:

- **Only when there is no keyword.** "AI roles using Kafka" is still a search
  for Kafka. Keywords decide relevance whenever there is one (§14).
- **Only if a title has every word.** Otherwise the old search stands, vectors
  included. `test_no_title_with_the_word_falls_back_to_the_old_search`.
- **Word forms are compared by stem, on both sides.** `tokenize` keeps
  "engineering" and "engineer" apart, and "data engineering roles" is asking
  for data engineers. `_stem` only has to be consistent, not correct.
- **A kind is a known role, or a title two postings share.** A title the role
  table recognises is the kind it names. One it does not is its own words with
  the level and qualifiers off ("product manager"); a one-off goes under
  "other titles", or 157 postings with 98 distinct titles would be 98 headings.
- **The headings are ordered for the reader**: more of what was asked first
  ("product manager" before "engineering manager"), then a kind the table
  knows before a family of titles, then by size, one-offs last.

This also reaches roles the table does not hold. "product manager jobs" was a
keyword search with both words dropped; it is 247 titles now, product manager
147 and product marketing manager 47.

### Words that join a role to its subject

The owner asked "any jobs role based on ai" on 2026-10-06 and was shown one
posting, "Enterprise Sales Executive, AI Solutions - US-Based", where "any
roles with AI" lists 222. `tokenize` drops "on" and keeps "based", and the
title search above wants every word asked for in the title.

"based" is in 0.18% of the owner's titles and 70% of their descriptions. The
same held for the other connectives measured that day: "focused", "require",
"use", "include", "within", "relevant", "specific", "driven" and their forms,
each in under 0.05% of titles. They are framing words now (`_FRAMING_WORDS`),
the list §14 keeps for words rarity cannot be trusted to remove.

It fixed a second fault with the same cause. The ones in under 30% of
descriptions were being searched for there: "roles focused on security"
dropped "security" (36%) as too common and returned postings that say
"focused". It lists 153 security titles now.

Left out on purpose: "field" (1.2% of titles, Field Engineer), "area" (Area
Manager) and "level". Unchanged: a question with a rare word is still a
keyword search, so "roles requiring Kafka" returns what "Which open roles use
Kafka?" does. `tests/test_chat_connecting_words.py` holds all of it.

## §27: routing

Routing, each from one word (`tests/test_chat_intent_routing.py`,
`tests/test_retrieval_names.py`):

- **"artificial intelligence" is read as "AI"** (`_SPELLED_OUT`). Only that
  pair: no title spells it out and "AI" is never searched for in
  descriptions, so nothing is lost one way round. "machine learning" and "ML"
  are both in titles and are not in the table.
- **A first-person question about applications is not a search for a
  title.** "review" is in a title and "waiting" is in none, so one wording of
  "what needs my review" was searched and the other was not. It needs the
  noun ("applications", not "apply"), a word for where one stands, and the
  first person: two of the three are in "Applications Engineer I", and with
  the verb counted "jobs I can apply to that use a message queue" lost
  "queue".
- **"target" is a framing word**, and **a question that names nothing is not
  searched** (`Retrieval.unnamed`). It used to be reported as "searched 0 of
  11,147, 11,147 not searchable yet".
- **A company's name is its words in order.** "AI-native" is not Native AI.
  A common word after "at" is a name only where a name would stand: "at
  fast-growing firms" is not Fast.
- **A gap question is about the owner** (`chat.asks_about_gaps`). The gap
  words alone put the résumé's skills into the context of "Which jobs require
  Python skills?", and sent them to a remote provider when one was picked.
- **A rare word names a company with nothing open only after "at"**
  (`_only_others_say_it`, 2026-10-12). The owner asked "any posting on
  postman" and "any posting mentioned postman" and was told there were no
  open postings at Postman: a company on the sheet with no board, and a tool
  32 open postings at 20 other employers name. The word is searched for, and
  the context still says the company of that name has nothing open
  (`Retrieval.not_hiring`), so both readings are answered. "jobs at Postman"
  is still the company. A company with a posting open is unchanged, and so is
  a word no posting says. On the registry that day 213 one-word names were of
  this kind, "Index", "Assembly", "Drop" and "Pick" among them, with SAP,
  Zendesk and DoorDash. Not fixed: "post man" typed as two words, and a tool
  named like a company that *is* hiring ("managing Okta administration" is
  still Okta's own postings).

## §27: the semantic experiment

The semantic experiment, still off (`CHAT_SEMANTIC_CANDIDATES`):

- **What the keywords found keeps its order.** A posting whose title holds
  two of the question's search terms is held where it is
  (`ANCHOR_TITLE_TERMS`, chosen on dev questions and then run on 80 others);
  semantic candidates merge by rank below it. Held only when a second list
  or a second model could move it, so with both off the order is the keyword
  order. Two always: letting a question's only search term be enough held
  thirty Network Engineer titles ahead of a Customer Experience Manager
  role, because "network" was the one word rare enough to search for.
- **The second model reads the title above the chunk.** A chunk never
  contains it. `main` reached the same conclusion from its own measurement
  and drew the line one step finer (§21): a model that *judges* reads the
  title, company and place over the chunk, and a model that scores likeness
  reads the chunk alone, which is the arrangement the 2026-10-05 numbers were
  taken on. That is what is in the tree, and
  `tests/test_chat_cross_encoder.py` holds both halves.
- **No similarity cut-off.** There is no graded set to find one on.

### Two things the merge with `main` left between its halves

Found on 2026-10-12 by reading #140 after it landed. Each half was right
where it was written. The fault was at the join, and no test spanned it.

Added to the root file's §27 on `main` after these notes were moved; the
other of the two is in `apps/api/CLAUDE.md`.

- **A title is held from a likeness model, not from one that judges.**
  Holding was measured with the embedding re-ranker. #139 measured the
  cross-encoder being shown every keyword match under its title, its order
  fused with the search's. Merged, the title was held from the judge too: a
  third arrangement nobody ran. Each kind now does what it was measured
  doing. `tests/test_chat_cross_encoder.py` holds both.

## Two things a question asks that are read, not searched for

Added 2026-10-13, after the notes above were moved here. `docs/RAG_PIPELINE.md`
has the measurements; this is what a later change could quietly undo.

**"Which jobs sponsor?"** (`matching/sponsored.py`, #155.) "jobs that sponsor
H-1B" had listed five postings that all say they do not: the word was the
search. A question asking which postings sponsor is answered from what each
posting states, read by `eligibility.py`.

- **A posting that does not say is not an offer.** 4,855 of 5,204 in-area
  postings say nothing. They are counted and not listed.
- **The owner's own question is still refused.** "Do I need sponsorship?" is
  §2.2 and reaches no search. A negated question is left to the search as it
  was.

**A level** (`matching/level.py`, #156.) "entry level software engineer jobs"
listed three senior postings and "internships in data science" nothing.

- **A title's level is the feed's** (`filters.detect_seniority`); a
  question's is a shorter list. "architect", "director", "lead" and
  "associate" are a level in a title and the job in a question, so a question
  that says one is naming a title.
- **The level's words are not search terms**, and a question that is only a
  level ("internships") is still a question about postings.
- **A title naming another level is never listed.** One naming none is left
  out and counted, or listed after with `CHAT_ALLOW_UNKNOWN_SENIORITY=true`.
  Nearly half the in-area titles name none, so the count is part of the
  answer either way.
- **The level is applied before the cut to thirty**, with "Sr." counted as
  "Senior". Each of those was a benchmark target lost when it was not.

`tests/test_chat_sponsorship.py` and `tests/test_chat_seniority.py` hold them.

## A filter runs before the cut

Added 2026-10-18, Phase 5 of `docs/HARNESS_AUDIT_2026-10-15.md` (finding F,
"filter-before-limit starvation"). Each pool above was read to a fixed size,
best first, and only then asked about the area or the level: 120 keyword
matches for the area, 500 for a level, 500 for a sponsorship question, the
newest 30 whole titles, the nearest 200 chunks, the nearest 40 posting
vectors. More dropped candidates than that, all ranked above one kept, and
the kept one was never read. The answer said "none in the search area" or
"0 state they sponsor".

`_read_until` (ids in order) and `_nearest_kept` (chunks by distance) read
the old size first and then on, doubling, until enough candidates are ones
the filters keep, or there are none left. `tests/test_retrieval_candidate_limits.py`
aims one test at each pool, and each failed before the change.

What a later change could undo:

- **The old size is read first, whatever it holds.** A search that was not
  short reads exactly what it did, so its counts and order are unchanged.
- **Every posting read is returned, kept or not.** The area's "left out"
  count and the level's count are of what was read, as they always were; a
  longer read makes them larger and nearer the truth.
- **A level counts only titles at that level**, not the ones naming none
  that `CHAT_ALLOW_UNKNOWN_SENIORITY` keeps. Those are listed after, so they
  cannot fill the thirty in place of one at the level.
- **A sponsorship question reads every posting that says its word**, as the
  role and mention paths already did. Its counts are of that set.
- **A question asked only by a title abroad is still a question.** The
  whole-title lookup returns what it read, abroad included, so the gate that
  asks whether anything was named is unchanged.

Measured on the live database on 2026-10-18 in one read-only snapshot, the
old and new search over 216 everyday questions (30 skills in seven forms,
six with no rare word): 176 unchanged, 5 empty answers now answered (four
sponsorship questions and "internships using kubernetes"), 20 more with a
different five, 2 reordered, 13 with only their counts changed. No question
shows fewer postings, and no posting the old search showed is missing from
the new answer or its further list. The changes are in junior, entry-level,
internship and sponsorship questions, where the filter keeps few: "junior
python jobs" found 4 at the level and now 34. The known-target benchmark
(`make eval-retrieval`) is identical on both sets. Median time 0.56 s to
0.58 s, slowest 1.5 s to 1.2 s. The report is
`docs/harness-audit/phase-5-retrieval.md`.
