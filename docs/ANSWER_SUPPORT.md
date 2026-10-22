# Reviewing assistant answer support

Retrieval relevance and valid citation labels do not prove that an answer is
true. `apps.api.answer_support` provides an **offline manual evaluation**, not
a runtime guard, an automated judge, or a new retrieval mode. It imports the
existing chat preparation/reply code; the live routes do not import it.

In a local evaluation harness, keep the `Ready` returned by `chat.prepare`,
call the chosen provider through the existing local-only safety harness,
then pass that same `Ready` and `ChatReply` to:

```python
from apps.api.answer_support import capture
record = capture("a-stable-question-id", ready, reply)
```

This makes no model call or database read. Save an array of `record.model_dump()`
under **gitignored `storage/evaluation/` only**. It includes the entire prompt
and answer, which can contain résumé, application and mail PII. Do not attach
it to a public issue, PR, fixture or log. Saved historical answers can instead
use `freeze` with their exact system, prompt, answer, model and supplied labels.
Do not substitute current context for historical context.

Every nonblank answer line becomes a pending review, including headings and
closing remarks. Read **every claim on the line** against its cited posting,
or the relevant code-supplied counts/status. `supported` means all its claims
are supported. Use `contradicted` for a conflict, `unsupported` for an absent
fact, `uncertain` when the evidence is insufficient, and `non_factual` only
when the entire line makes no factual assertion. A correct citation does not
excuse confusing required skills with preferences, tooling or alternatives,
nor attributing global application counts to one employer.

For each reviewed line set `reviewer` (`owner` or `model`), an explanatory
`note`, and `quotes` copied exactly from the frozen prompt. Supported and
contradicted verdicts require quotes; quotes alone do not establish entailment.
Agent-produced reviews must say `model`, never `owner`. Edit only review
fields. The snapshot hash detects changes to frozen input; it is a consistency
check, not a signed attestation. Loading refuses omitted, reordered or edited
answer lines, absent evidence quotes and supported invented citation labels.

```sh
.venv/bin/python -m scripts.score_answer_support storage/evaluation/answers.json
# During review; incomplete coverage is explicit:
.venv/bin/python -m scripts.score_answer_support storage/evaluation/answers.json --provisional
```

The command reads only storage_root, calls neither a database nor a model, and
prints aggregate counts only. Incomplete reviews fail unless `--provisional`.
The support rate is supported **lines** divided by all reviewed factual lines;
uncertain counts against support, nonfactual lines are excluded. Empty answers
are unscorable and make a report incomplete. Owner/model review counts are
separate; this is not an owner-validated accuracy result when model grades
are included. Keep candidate/baseline cohorts and reviewer policies identical.

No release threshold or default behavior changes with this tooling. A later
promotion needs a frozen, representative answer set and owner review.
