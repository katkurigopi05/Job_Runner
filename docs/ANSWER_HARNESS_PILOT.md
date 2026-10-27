# Bounded answer-selection pilot

This pilot adapts Harness2's Propose–Probe–Compose–Reflect experiment design to
Jobrunner's existing guarded Kaggle provider. It does not install the upstream
agent executors or grant an agent shell access. The reference revision is
[`1cfe254e`](https://github.com/google-research/harness2/tree/1cfe254e8d25b15f37048c3730134686abe60335).
The upstream credential-free demo is a separate synthetic demonstration; its
success is not evidence of improved Jobrunner answers.

The pilot requires the strict evidence-selection and Kaggle readiness changes.
It runs outside the live request path. It never changes production prompts,
provider configuration, deterministic facts, evidence validation, privacy rules
or retained memory. The baseline remains selected until independent source
review. Protocol-valid evidence IDs are not an accuracy score.

## Prepare and run

Use a previously captured, approved frozen JSON case set containing `facts`,
`selection` and optional `prose` cases. It must contain the exact current system,
prompt, retrieval state and evidence catalogue. Snapshots with explicit mail
sharing or mail-context flags are refused. This runner does not query postings
or recruiter mail. Live calls read only local contact identifiers through a
read-only database connection for the existing privacy guard.

Select the same settings file as the installation that holds the private
snapshot, Kaggle connection and daily audit/quota record. The CLI anchors
relative storage and vault settings to that file's directory, including when
the code runs from an isolated worktree. The output directory must be new and
inside the selected `storage_root`; input files must be owner-private regular
files. Nothing containing prompts or model prose is printed to stdout.

```sh
export JOBRUNNER_ENV_FILE=/absolute/path/to/installation/.env
.venv/bin/python -m scripts.run_answer_harness prepare \
  /absolute/path/to/storage/evaluation/local_cases.json \
  /absolute/path/to/storage/evaluation/pilot-01
.venv/bin/python -m scripts.run_answer_harness baseline \
  /absolute/path/to/storage/evaluation/pilot-01
.venv/bin/python -m scripts.run_answer_harness pilot \
  /absolute/path/to/storage/evaluation/pilot-01
.venv/bin/python -m scripts.run_answer_harness status \
  /absolute/path/to/storage/evaluation/pilot-01
```

`prepare` performs no model call or database read. It freezes the input,
deterministic split, execution-code fingerprints and request limits. Later
commands refuse changed input, code or plan identity. A fixed seed selects six
development and six heldout semantic cases; other semantic cases stay reserved.
This is a question-level split; posting sources can overlap. The split is a
small feasibility pilot, not a representative accuracy estimate.

`baseline` captures every frozen case before any refinement. Facts are copied
without calling a model; semantic cases run three times. The current 54-case
set requires at most 91 requests (30 semantic cases × three, plus one prose
case). Inference uses only the existing privacy-protected Kaggle provider with
its readiness, audit, quota and 4096-token context checks. There is no fallback.

`pilot` requires the complete matching baseline. It chooses two policies from a fixed
local allowlist, probes each on development cases, chooses a composed policy and
probes that variant on development and heldout cases. Every semantic probe runs
three times. Improvement and reflection receive development evidence only;
heldout outputs never become improvement feedback. Improvement/reflection JSON must
be exactly `{"guidance":"policy-key"}`, where the key is `all_constraints`,
`direct_evidence`, or `conservative`. The runner expands keys into immutable,
query-neutral instructions locally. Arbitrary generated instruction text is rejected
before any probe; examples cannot add a Kafka or other topic constraint to unrelated
questions. Composition selects one approved policy rather than writing new text.
All source material stays
intact; an oversized request fails rather than silently truncating evidence.

The pilot permits at most 76 requests: 54 development probes, 18 heldout probes,
three improvement requests and one reflection request. The baseline plus pilot
therefore permits at most 167 requests for this frozen set, subject to the
existing shared daily quota. Each phase has a 3600-second inference budget,
including in-flight requests. Selection output is capped at 180 tokens;
improvement output is capped at 256. This is a request ceiling, not a measured
latency or cost prediction.

## Results and recovery

`baseline.json` and `pilot.json` record operational state and private results.
Private JSONL journals checkpoint attempted calls and raw results. Files use
owner-only permissions and phase locks prevent concurrent writers. Failures
record exception types, not potentially private exception messages.

Completed phases are returned without more inference. A readiness failure with
zero attempted model requests can be retried after reconnection; the previous
state is preserved. A partial inference run is not automatically replayed.
Preserve its checkpoints and prepare a new output directory after reviewing
the failure and remaining quota. A stale lock requires inspecting whether its
process is still active before manually removing it.

`baseline_reviews.json` and `candidate_reviews.json` use the existing
`AnswerReview` format for model-dependent answers. Fixed code responses remain
in the baseline response artifact, including protected refusals that have no
model context. Reviews freeze the exact sent prompt and code-rendered answer.
All review lines start pending. Review every claim against source
quotes; retain owner/model attribution. Run the existing
`scripts.score_answer_support` from the selected installation directory to
score completed private reviews. Model reviews do not establish owner-validated
accuracy.

Reflection produces an **unreviewed** lesson inside the private result. It is
never installed in runtime memory. The output always retains `chosen=baseline`
and `promotion=requires_owner_review`. No candidate is automatically promoted,
including one with better protocol-validity counts. Compare semantic support,
useful coverage, abstention, latency and total inference work before considering
a separately reviewed change.

New plans use schema version 2 and freeze separate prose and selection system hashes, plus the prompt registry source. Existing version-1 artifacts remain unchanged and require their original pinned checkout; do not rewrite them to bypass identity checks. New selection snapshots must carry the dedicated selection system, and prose snapshots must carry the prose system. Prose captures reject selection-protocol JSON rather than presenting it as an answer.
