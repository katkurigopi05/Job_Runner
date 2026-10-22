# Opt-in context-budget evaluation

`chat.prepare` accepts an optional `context_budget` for local evaluation
harnesses. The HTTP and streaming routes **do not pass it**. Live defaults,
the system prompt/version and provider routing are unchanged. This is a
packing experiment to measure before any production promotion.

```python
from apps.api.chat_budget import BudgetExceeded, PromptBudget
from apps.api.routers import chat

# count_chat_tokens(system, user) is supplied by the harness. It must count
# the chosen model's complete message template and assistant generation prefix,
# not just the raw strings. Load its tokenizer locally, without remote code.
budget = PromptBudget(
    counter=count_chat_tokens,
    tokenizer_id="model + tokenizer revision/hash + template hash",
    context_tokens=4096,
)
try:
    ready = await chat.prepare(body, session, context_budget=budget)
except BudgetExceeded:
    # Record a refused case. Do not call a model, switch provider, or cut facts.
    ...
```

Preparation still enforces protected-question routing, mail withholding and
retrieval normally. A code refusal/crawl command still returns `ChatReply`,
so check for `Ready` before accessing `ready.budget`. This API makes no model
call and loads/downloads no tokenizer itself. A harness must also keep the
existing local-only endpoint/model checks and memory watcher for inference.

The packer reserves 600 answer tokens (the existing `ANSWER_TOKENS`) and 128
safety tokens. It measures the complete prompt, including the unchanged
system text. If it fits, the prompt remains byte-identical. Otherwise it
removes **entire excerpts**, lowest-ranked first, each replaced with an
explicit withholding line, until the rendered prompt fits. There are at
most N+1 measurements for N supplied postings.

All posting labels, metadata, application statuses, skill strength/alternative
evidence, gaps, exact counts and scope, withheld-data notices, selected
application/mail context, earlier conversation and the current question
remain mandatory. Earlier-turn references are not silently dropped to fit.
No string slicing, partial claim truncation or question-text parsing selects
what to remove. The renderer owns the optional excerpt fields. Retrieval,
the shown/further source lists and their order remain identical.

If the remaining mandatory context cannot fit, `BudgetExceeded` refuses the
experiment before generation. The budget report holds token counts, the
declared tokenizer identity and withheld excerpt labels; no private text.
Treat the result as a hypothesis about available evidence, not a guarantee
that the answer will remain correct. Pair packed/unpacked answers using
claim-support reviews before enabling a production policy.

The token counter is an explicit trust boundary: it must return nonnegative
integer counts and be calibrated to the selected provider's actual tokenizer,
chat template and thinking configuration. Word counts in unit tests exercise
allocation only. Character/word ratios are not token counts. An offline
reference-tokenizer measurement is not native Ollama calibration. Compare
against `prompt_eval_count` in a separately memory-gated inference run before
production use; keep safety space even after calibration.

Private prompts, tokenizers and evaluation artifacts belong under ignored
`storage/evaluation/`. Do not commit contexts or application details to a PR.
