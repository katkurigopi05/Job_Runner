# Graph retrieval in the assistant

The assistant can traverse a source-backed knowledge graph for two standalone
questions, available as prompt buttons in the dashboard:

- **Jobs from my projects:** “Which jobs match skills from my projects?”
- **What am I lagging on?:** “Which skill gaps recur across jobs in my area?” (“…across my strongest matches?” is read the same way)

The projects question also accepts one fully recognized suffix: `in California`,
`in the Bay Area`, `in San Francisco`, `in London`, `at Acme` (when the registry
knows that employer), or `for backend engineer roles`. Place names use the shared
locality vocabulary and office rules; companies use the assistant's existing name
resolver; role aliases come from `roles.py`. Unknown names, additional skill or
level constraints, and mixed suffixes keep normal retrieval. No part of a suffix
is silently discarded. The gaps question remains unchanged.

Both the saved-link path and the top-100 fallback apply the constraint before
selecting results. Answers name the applied scope; a place abroad waives the
standing search area, and a recognized employer with no matching open jobs gets
a scoped empty answer. These are retrieval constraints, never match scores.

The earlier prompt, “Compared with my résumés, which skills do my top matches ask
for that I am missing?”, also routes through the graph. Graph answers show a
**GraphRAG** label above their evidence details. Existing saved answers retain
their original retrieval path; rerun the question to use the updated route.

The graph connects the candidate to base résumés and eligible public GitHub
projects, those sources to normalized skills, and directly required skills to
jobs and their companies. A project match traverses
`candidate → project → skill ← job → company`. Gap retrieval follows job
requirements back to skill nodes without a base résumé connection. It reports
public project support separately, so a résumé omission does not erase project
evidence or imply that the owner lacks the skill.

The existing assistant model selects from the retrieved evidence IDs. The server
renders the selected paths and posting citations from source facts; model-written
claims and unknown IDs cannot become evidence. Both `/chat` and `/chat/stream`
use this path. Empty cached results return a scoped factual explanation without a model.

Project answers show up to five jobs, their shared technologies, and which
projects support each technology. Other direct requirements without evidence in
the checked projects are stated separately. Full requirement quotes, commit links,
check times and coverage diagnostics are available under **Evidence details**.
The footer identifies the top-match sample rather than implying that it searched
the entire open-posting corpus.

## Live GitHub checks with Kaggle

Select **Kaggle** in the assistant and use either graph prompt. Before answering,
the model requests `github_project_evidence` tool calls through a bounded JSON
protocol. Job_Runner discovers and executes the read-only tool through an
in-process MCP client/server connection. Kaggle's existing text transport handles
the planning and selection rounds; native notebook tool support is not required.
The same privacy-wrapped provider instance and verified model serve both rounds.
The existing Kaggle model helper must be ready; use **Load Kaggle helper** in the
dashboard if connection status reports `helper_missing`.

The host offers up to 20 eligible imported public repositories, most recently
pushed first. The model may request at most five distinct repositories. Project
IDs must be in that catalog and the only allowed tool is the read-only GitHub
evidence tool. GitHub credentials remain on the host; Kaggle receives anonymous
project references and normalized skills, not credentials or source files.

Each tool call rechecks the repository's identity and public visibility, resolves
the current default-branch commit, and reads dependency/container/CI files at that
exact SHA. File and byte limits and a 60-second per-repository deadline bound the
work. GitHub's reported primary language is metadata observed at check time;
manifest evidence is pinned to the commit. Partial reads and failed checks are
reported. The answer includes commit SHAs, check times and commit-specific project
links. Local changes must be pushed to GitHub before this path can see them.

The graph uses only successfully checked project snapshots for that question.
Failed or unselected repositories never silently fall back to cached technologies.
The local project cache and owner curation remain unchanged. Even an empty answer
is marked remote if Kaggle was called to plan the checks. Other assistant providers
continue to use cached project facts.

The tool is also exposed on the existing `jobrunner` MCP server, which reaches it through the API (`GET /projects/{id}/live-evidence`) like every other tool there (`tests/test_mcp_ids.py`). A standalone
GitHub-only server can be run with `.venv/bin/python -m apps.mcp.github`. It exposes
no write, shell, arbitrary-URL, or résumé-linking tool.

## Sources and scope

PostgreSQL remains the source of truth. The graph is built in memory for each
question from current rows, so no graph service, index backfill, migration, new
package or provider configuration is required. This is a domain-specific graph
retriever, not an installation of Microsoft's GraphRAG pipeline. Its graph and
document evidence combination follows the general
[local graph search approach](https://microsoft.github.io/graphrag/query/local_search/).

Once the skill links are saved (`make skill-links`, `packages/matching/skill_links.py`), the
projects question reads every open, unskipped posting in the configured search area that directly
requires a technology the projects declare, ordered by how many it shares and then the newest, and
says how many open postings are not linked yet. Both questions then read a linked posting's
requirements from the saved links instead of extracting them again; a link made from older text or
by another extractor is not trusted and that posting is read as before. The gaps question likewise reads every linked open posting in the area that states a
direct requirement, loading a description only to admit an on-site posting as remote. Until
links exist, retrieval examines at most the top 100 open, unskipped matches in the
configured search area. A posting matched under multiple profiles counts once at its best
score. Results are explicitly described as a sample. Area filtering happens
before taking 100 eligible postings, using the feed's shared location rules.

Project technologies come only from the source-reported primary language and
dependency declarations. Forks, private repositories and explicitly excluded
projects are omitted. Names, topics and descriptions cannot create technology
edges. A declaration establishes a repository connection, not proficiency,
employment experience, or complete eligibility for a job.

Job edges use the existing versioned requirements extractor and evidence rules.
Only direct requirements count; preferred skills, alternatives and unclassified
stack mentions do not. Stale extraction is refreshed in memory; unread jobs are
reported. Project results rank by the number of shared directly required skills,
then existing match order. Recurring gaps must occur in at least two distinct
sampled jobs and rank by frequency. Up to eight evidence items reach the model.

Only normalized skill names, anonymous project references, tool schemas, freshness
metadata and public posting evidence enter the model context. Base résumé prose, project names and URLs,
private repositories, application details and mail are excluded. Local rendering
restores public project attribution and links. Existing per-question provider
choices and no-fallback behavior still apply.

## Job-first project tailoring

Resume Studio (`/resumes/studio`) supports the reverse direction: paste a job,
check the current public repository inventory through GitHub MCP, rank projects
by the number of directly required technology declarations, then optionally ask
Kaggle 27B to rewrite selected descriptions. Pins take priority; recency and
vocabulary break coverage ties. Preferences and alternatives do not count as
direct requirements. Automatic selection omits zero-overlap projects when direct
requirements are readable. The existing relevance ranking remains the fallback
for postings without readable direct requirements.

Forks are excluded from ranking, selection, rewriting and Studio saves, including
explicitly included or pinned forks and manual project-ID requests.

Live Studio checks are capped at 20 imported public, non-fork repositories, with
pins first. Failed checks are excluded, partial coverage is reported, and cached
facts never fill missing live evidence. Private projects require live checking
off and the existing owner inclusion rules. GitHub descriptions are current
repository metadata; technology declarations come from the checked commit.

The original and proposed description appear together. Each rewrite is guarded
against that project's own source; another project's achievements cannot back
it. A missing description stays missing. This does not infer implementation
details or achievements from arbitrary source code. The chosen model also
revises eligible résumé bullets through the existing targeted tailoring flow.
Kaggle is explicitly selected and does not fall back to another model.

Studio offers replacement of the base résumé's Projects section with the ranked
descriptions. This can shorten or remove detailed source project entries, so the
option is visible before generation and the print preview shows the result.
Saving rechecks descriptions against their sources and stores a separate résumé
version; imported projects are unchanged. Saved PDF and DOCX retain the approved
wording even if a later GitHub sync changes the imported descriptions.

## A project question about a topic

"Which jobs match my projects related to RAG pipelines?" (or "…about fraud detection for
payments") finds the jobs by meaning. The topic is embedded on this machine with
Qwen3-Embedding-4B (`query_vectors.embed_question`, unloaded at once), the 200 open postings
nearest to it (`graph_questions.TOPIC_NEAREST`, through `chunk_search.ShortlistRanking`) become
the constraint, and the graph keeps those that directly require a project technology, listing
the most related first. A posting about the topic need not use its words. When Qwen cannot
embed the topic the question is refused with the reason, and nothing is searched another way.
A topic ending in a known place, employer or role ("…related to RAG in London") is not a topic
question, since that part would be searched by meaning and never enforced. On 2026-11-19, RAG
pipelines: 41 of the 200 nearest need a project technology, 3.8 s including the model load.

## The skill-gaps question takes the same constraints (2026-11-19)

"Which skill gaps recur across jobs in my area for data engineer roles?" counts the gaps over
the IT jobs the constraint keeps. Both questions share one suffix parser
(`graph_questions._constraint`): a place, an employer, a role from the role table, or a topic,
and a suffix it cannot read in full is not a graph question. The gaps search applies
`GraphConstraint.keeps`, which reads no description, before the area rule, and a place abroad
waives the area as it does for projects. A topic lists its most related jobs first, so each
gap's example requirement comes from one of them.

Measured read-only on the live database, 2026-11-19 (warm):

| Suffix | IT jobs read | Top gaps (jobs) | Time |
|---|---|---|---|
| none | 2,151 | Kubernetes 283, GCP 201, Go 69 | 2.1 s (cold) |
| for data engineer roles | 65 | Databricks 7, dbt 6, Kubernetes 6 | 0.44 s |
| for backend engineer roles | 19 | DynamoDB, GCP, Go, Ruby (2 each) | 0.48 s |
| in California | 758 | Kubernetes 109, Go 32, Terraform 27 | 0.48 s |
| in the Bay Area | 682 | Kubernetes 95, Go 31, PyTorch 25 | 0.41 s |
| at NVIDIA | 402 | Kubernetes 74, Bash / shell 23, PyTorch 23 | 1.47 s |
| in London | 0 | none of its 31 open postings states a direct requirement | 0.41 s |

A role counts titles the role table reads as exactly that role, so "backend engineer" is
narrow. Not measured live: a topic, which loads Qwen3-Embedding-4B; it is the projects topic
path, measured above.

## The graph picks the base résumé (2026-11-21)

`pick_resume.choose_base_resume` starts from the readable base résumé that backs the most of a
posting's direct requirements, read with the graph's own rule (`skill_links.direct_requirements`,
`is_direct`), before similarity is consulted; ties fall back to similarity and the owner's
standing choice. On 200 in-area IT postings the pick changed on 11 (6%), each gaining one
backed requirement. Details in `packages/matching/CLAUDE.md`.

## Current limits

This first version requires exactly one candidate; it refuses to combine multiple
candidates' evidence. Both questions read every linked open posting in the search area; before
`make skill-links` has run they read the top 100 scored matches. The two explicit standalone questions
activate graph retrieval, including the supported suffixes above on either question.
Unsupported constraints keep the existing retrieval path. Community summaries, graph visualization and
arbitrary graph questions are outside this version.

Validate with `.venv/bin/pytest -q tests/test_graph_rag.py tests/test_graph_constraints.py tests/test_graph_gap_constraints.py tests/test_graph_topic_questions.py tests/test_graph_rag_saved_links.py tests/test_github_live.py tests/test_github_tool_chat.py`. Database tests use the
suite's separate marked test database; they never reset the live database.

## Extractor version 5

Graph answers count only direct requirements, so "Python and/or Golang" must read as a choice, not as
requiring Python (`evidence._AND_OR`, `requirements.EXTRACTOR_VERSION = 5`). The bump makes every stored
reading at version 4 stale: readers re-extract it in memory until `make extract-requirements` rewrites it,
and every saved skill link is stale until `make skill-links` runs again.


## Optional Kaggle source prose (2026-11-19)

`CHAT_KAGGLE_PROSE=false` is the shipped default. When explicitly enabled, a
valid nonempty Kaggle evidence selection may make one additional call through
the same privacy-wrapped, pinned and audited provider. Other providers and
refusals retain their existing paths. Only selected source evidence and code
counts enter that call; graph evidence keeps anonymous project references.

The answer is buffered and checked before either chat route displays it: each
of at most six sentences cites a selected source, every number is already in
the selected evidence or computed counts, and the factual wording is copied
from those sources. Novel paraphrases are deliberately rejected because a
capitalization-only name check cannot rule out invented company/job titles.
On any validation, provider, or context-budget failure, the existing code
answer appears with a fixed one-line note. This never invokes another model.
The Kaggle helper checks actual prompt plus answer tokens against its 16,384
token context. Explicit offline pack callers preserve all selected evidence or
skip prose; the prose answer reserves 600 tokens. The flag is not enabled in
the owner's environment by this change.
## IT scope for recurring gaps (2026-11-19)

The gaps question counts only IT jobs in the configured search area, including
product/program and technical pre-sales through the shared `it_titles.is_it_role`
reader. Sales, office administration, UX, and hardware roles are outside that
scope. Both saved-link retrieval and the top-100 fallback apply it before
counting or taking the sample. Jobs from projects keeps its existing scope.

## Saved project selection in apply (2026-11-21)

Apply and batch tailoring read the owner's saved `project_skills` rows for
job-first selection. Studio's saved-facts path reads the same rows; its live
GitHub path continues to use the declarations returned by that live check.
Both use `github.select.projects_for_posting`: pins first, then the number of
direct requirements backed by language/dependency declarations, with the
existing relevance/recency/substance tie breakers. Forks and excluded projects
never qualify. Missing saved links do not fall back to project prose or topics.
When direct requirements are readable, automatic selection drops zero-overlap
projects except pins, before taking the limit. Explicit Studio selections stay
the owner's choice. Without direct requirements, each caller retains its prior
fallback (apply's optional semantic relevance, batch's salient terms, Studio's
role-preset vocabulary). Selection reads no GitHub endpoint and needs no model
when direct requirements are available.

This changes selection and order only. It does not write a skill or widen the
facts that may back any bullet; the existing per-project fabrication guard still
decides. Cache identity now preserves project order, with a revision bump so an
old PDF cannot silently bypass the new ordering.

Read-only measurement against the live database, with worktree code explicitly
selected through `PYTHONPATH`, 2026-11-21: first 100 posting UUIDs from the 2,151
open, unskipped, in-area IT postings with current saved direct requirements.
No GitHub, model, or embedding call; the previous pipeline's optional semantic
rescue was disabled, so this is a lexical-path comparison rather than a claim
about that rescue's results. The session was rolled back.

| Metric over 100 postings | Before | After |
|---|---:|---:|
| Selected projects per posting, mean | 4.00 | 1.49 |
| Direct requirements covered, summed per posting | 51 of 249 | 51 of 249 |
| Fully covered postings | 7 | 7 |

67 selections changed, all by removing zero-overlap projects: 251 project
placements removed and none added. No direct requirement coverage was gained
or lost. Reading and comparing the sample took 106.1 seconds; this includes
both selectors and is not a feed/request latency measurement.

## What stands behind a gap's count (2026-11-22)

"Kubernetes — directly required by 283 of 2151 open IT jobs" was a number the
owner could not check. Each gap the answer lists now opens, under the dock's
existing Evidence details, onto the jobs behind the count and the projects
declaring the skill (`matching/gap_evidence.py`, `components/gap-evidence.tsx`).
The answer itself only gains one sentence saying so.

- **The count is the jobs listed.** A gap's N is its distinct jobs
  (`graph_rag.one_per_job`): a job naming the skill twice counts once. The
  drill-down is built from the same graph edges, over the population retrieval
  handed answer construction, so it cannot count differently.
  `GapEvidenceOut` refuses a total that is not its distinct ids.
- **The population is fixed when the answer is written.** The reply carries all
  N ids and the first 10 jobs with their requirement lines. The rest are read on
  request by those ids (`POST /chat/gap-jobs`, 50 at most a call): nothing is
  searched again and no job joins. A job read again says whether it still
  counts (`current`, `closed`, `changed`). The dock shows "Showing 10 of 283"
  and pages through the rest.
- **Direct requirements only.** Preferences and alternatives never make an edge,
  so they never enter a list.
- **Projects are the eligible ones** (`graph_rag.is_eligible`): forks, private,
  excluded and other candidates' projects stay out.
- **A declaration is labelled a declaration.** Primary language or a dependency
  file, in Studio's categories (`code_signals.Classification`), attributed to
  the repository, pinned to a commit when a live Kaggle check read it this
  request. `GapProjectOut` refuses any category but `declared`. The dock says a
  declaration is neither code that uses the technology nor the owner's own
  work, and that contribution is confirmed in Resume Studio.
- **Local rendering only.** The drill-down is a reply field (`graph_gaps`),
  never in a prompt or in history; models still see anonymous project
  references. Older saved answers have no such field and render as before.

**Why there is no implementation evidence yet.** Studio's code findings (file
and lines at a commit, `implemented`) are made inside one draft's live check
for one job description and are not kept. Respecting that, chat shows
declarations only. The smallest follow-up to share richer evidence: when the
owner saves a Studio draft with live code evidence, keep its public,
commit-verified findings (project, skill, path, lines, commit, category; no
source text) in a table, and let the drill-down cite them as Studio's
`implemented` items with their permalinks, still behind Studio's
contribution confirmation. `GapProjectOut` would gain those citation fields,
and its validator would accept `implemented` only with them.

Measured read-only on the live database, worktree code via `PYTHONPATH`: 8 gaps
over 2151 open IT jobs, each with ids equal to its count (Kubernetes 283, GCP
201, Go 69, … PyTorch 46 with 4 repositories declaring it in dependency files).
