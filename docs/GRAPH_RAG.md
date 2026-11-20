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

## Current limits

This first version requires exactly one candidate; it refuses to combine multiple
candidates' evidence. Both questions read every linked open posting in the search area; before
`make skill-links` has run they read the top 100 scored matches. The two explicit standalone questions
activate graph retrieval, including the supported project-question suffixes above.
Unsupported constraints keep the existing retrieval path. Community summaries, graph visualization and
arbitrary graph questions are outside this version.

Validate with `.venv/bin/pytest -q tests/test_graph_rag.py tests/test_graph_constraints.py tests/test_graph_rag_saved_links.py tests/test_github_live.py tests/test_github_tool_chat.py`. Database tests use the
suite's separate marked test database; they never reset the live database.

## Extractor version 5

Graph answers count only direct requirements, so "Python and/or Golang" must read as a choice, not as
requiring Python (`evidence._AND_OR`, `requirements.EXTRACTOR_VERSION = 5`). The bump makes every stored
reading at version 4 stale: readers re-extract it in memory until `make extract-requirements` rewrites it,
and every saved skill link is stale until `make skill-links` runs again.


## IT scope for recurring gaps (2026-11-19)

The gaps question counts only IT jobs in the configured search area, including
product/program and technical pre-sales through the shared `it_titles.is_it_role`
reader. Sales, office administration, UX, and hardware roles are outside that
scope. Both saved-link retrieval and the top-100 fallback apply it before
counting or taking the sample. Jobs from projects keeps its existing scope.
