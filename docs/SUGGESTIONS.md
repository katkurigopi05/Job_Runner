# Suggestions

Ideas that came from outside material and were judged worth doing. Each entry
says where it came from, what it changes, and how to tell it worked. Nothing
here is built until its status says so.

`docs/BACKLOG.md` is the gap register against the job-discovery spec. This file
is for everything else.

## Status key

- **open**: agreed as worth doing, not started
- **built**: done, with the commit or section that records it
- **dropped**: tried or re-read and not worth it, with the reason

---

## To work on, 2026-10-08

Added 2026-10-07. Items 1 to 3 come from an "Inference Engineering" cheat
sheet the owner shared; item 4 came out of evaluating Sentience Governor.
Suggested order is the order below.

### S1. Stream the assistant's answer (open)

**What.** The assistant waits for the whole answer before showing any of it.
`OllamaProvider._request` sends `"stream": False` (`packages/llm/provider.py:220`),
`POST /chat` returns one `ChatReply` (`apps/api/routers/chat.py:589`), and the
dock does one `fetch("/api/chat")` (`apps/web/src/components/assistant.tsx:382`).

**Why.** CLAUDE.md §14 measured about 5 s before the local model's first word
and 141 s for twelve questions, so about 12 s each. Streaming puts text on
screen at about 5 s. The model is no faster; the wait is.

**Things to settle before writing it.**

- `sources` is read out of the finished text (`cited_labels`), and the reply
  also carries `model`, `local`, `more_sources` and `matched_kinds`. Those
  cannot arrive first. They need a final frame after the text.
- The §2.2 refusal and the crawl command answer without a model and should
  stay plain JSON, or be sent as a single frame.
- Five providers implement `complete`. Either each grows a streaming method,
  or a provider without one sends its whole answer as one chunk. The second is
  smaller and keeps the remote providers unchanged for now.
- The audit entry is written before the request (`audit.record`). That does
  not change.

**Already learned in §17, and it applies here unchanged.**

- Next's dev proxy compresses and buffers a stream. The response needs
  `Cache-Control: no-transform`, with a test asserting the header.
- `LocalhostOnlyMiddleware` is a `BaseHTTPMiddleware`, so a client that leaves
  cancels the task. Anything touching the session has to be able to finish.
- `ASGITransport` cannot test a streaming endpoint. `tests/test_status_stream.py`
  runs uvicorn on an ephemeral port; copy that.

**Done when.** A question to the local model shows its first words before the
answer is complete, in a real browser through the dashboard proxy; cited
postings are still marked; `tests/test_chat_api.py` and the chat suites pass.

### S2. Record prompt-read and answer-write time per call (open)

**What.** Ollama's reply already carries `prompt_eval_count`,
`prompt_eval_duration`, `eval_count`, `eval_duration` and `load_duration`.
`OllamaProvider.complete` reads `message.content` and discards the rest.

**Why.** Two things the project currently measures by hand or not at all:

- Time to first token against time per token. §14's finding that MLX wrote
  faster and read the prompt slower took a separate benchmark to see.
- Silent truncation. §14 records that a prompt longer than `OLLAMA_NUM_CTX`
  is cut by Ollama without an error. A `prompt_eval_count` at or near the cap
  is the only sign of it.

**Where.** `packages/llm/provider.py` (read the fields), `packages/llm/audit.py`
(`AuditEntry` has sizes and digests, no timings). Counts and durations only,
which fits §10: nothing of the prompt is logged.

**To check first.** Whether `prompt_eval_count` on Ollama 0.40.0 reports the
whole prompt or only the part not reused from the previous request. If it is
only the new part, it cannot be used as the truncation signal on its own.

**Done when.** A local call's trail entry shows both token counts and both
durations, a test holds the fields, and a prompt built past the cap is flagged.

### S3. Try an 8-bit KV cache to double the local context (open)

**What.** Ollama can store the attention cache at 8 bits instead of 16:
`OLLAMA_KV_CACHE_TYPE=q8_0` with `OLLAMA_FLASH_ATTENTION=1`, set on the daemon.
Both are listed by `ollama serve --help` on 0.40.0. Neither is set on the
owner's machine. The default is `f16`.

**Why.** The cache grows in step with context length. For Qwen3 8B at 4,096
tokens the cheat sheet's formula gives about 0.6 GB (36 layers, 8 KV heads,
head size 128, 2 bytes; the model shape is from memory and should be checked
against `ollama show`). That fits the measured 4.8 GB held against a 4.1 GB
file. At 8 bits, 8,192 tokens of context should cost about what 4,096 does
now, which is headroom against the truncation in S2.

**Where.** A machine setting, not a repo change, until it is proven:
`launchctl setenv` then restart Ollama. If it holds, `ollama_num_ctx`
(`packages/core/config.py:106`) and `tests/test_ollama_local_model.py` change,
and §14 records the numbers.

**How to measure.** The same twelve questions §14 used, same judge, at three
settings: f16 at 4,096 (today), q8_0 at 4,096, q8_0 at 8,192. Report right,
partly, missed, wrong, total time, memory held and lowest free memory, as in
§14's table.

**Cautions, from the owner's standing rules.** One model loaded at a time.
Tell the owner before the load: an 8B model dips free memory far below its
size for a few seconds. Agree the free-memory floor first (30% was used on
2026-10-06). `ollama stop` and confirm with `ollama ps` afterwards.

**Done when.** The three-row table exists. Adopt only if q8_0 is no worse on
wrong answers and memory held at 8,192 is no higher than today's.

### S4. Make Claude Code ask before it approves an application (open)

**What.** A permission rule so an assistant driving the Jobrunner MCP server
has to stop and ask before these two calls:

```json
{
  "permissions": {
    "ask": [
      "mcp__jobrunner__approve_application",
      "mcp__jobrunner__submit_otp"
    ]
  }
}
```

**Why.** `approve_application` is the call that sends a real application.
§2.3's promise is kept by the review queue for a person at the dashboard. Over
MCP the one approving is a model, and nothing in this project's Claude Code
settings says it must ask. A log written afterwards, which is what Sentience
Governor offers, does not help once the form is sent.

**Where.** `.claude/settings.local.json` is gitignored, so a rule there
protects this machine only. `.claude/settings.json` does not exist yet and
would be shared through git, which is what reaches Claude Code mobile.

**To check first.** Whether an `ask` rule still prompts in the auto-accept and
bypass permission modes. If it does not, say so beside the rule.

**Optional.** `apply_to_url` matters only for a profile with `auto_submit` on.

**Done when.** A session asked to approve a parked application stops for
confirmation, and `docs/USAGE.md` says the rule exists.

---

## Looked at and not taken

Recorded so nobody evaluates them twice.

- **Sentience Governor** (getsentience.ai, Apache 2.0). Logs what a coding
  agent did against what it declared, and reports afterwards. Jobrunner's
  apply pipeline is fixed code, so there is no declared-versus-actual gap to
  measure, and `ApplicationEvent`, `FillReport`, `Receipt` and the LLM audit
  trail already record more and also enforce. Its traces would hold tool
  arguments in `~/.sentience/`, a second copy of résumé text outside
  `storage/`. S4 is the one idea kept.
- **The rest of the cheat sheet.** Continuous batching, chunked prefill,
  separate prefill and decode pools, multi-GPU parallelism, goodput and fleet
  monitoring all assume many users on GPU servers. Speculative decoding needs
  a second model loaded, which the 16 GB machine does not allow.
