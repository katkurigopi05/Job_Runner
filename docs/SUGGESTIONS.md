# Suggestions

Ideas that came from outside material and were judged worth doing. Each entry
says where it came from, what it changes, and how to tell it worked. Nothing
here is built until its status says so.

`docs/BACKLOG.md` is the gap register against the job-discovery spec. This file
is for everything else.

## Status key

- **open**: agreed as worth doing, not started
- **in review**: built, with the pull request that carries it
- **built**: done, with the commit or section that records it
- **dropped**: tried or re-read and not worth it, with the reason

---

## Worked on 2026-10-07

Added 2026-10-07 and built the same day, each in its own pull request off
`main`. Items 1 to 3 come from an "Inference Engineering" cheat sheet the
owner shared; item 4 came out of evaluating Sentience Governor. Each entry
keeps what was planned, followed by what happened.

### S1. Stream the assistant's answer (in review, #128)

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

**What happened.** Built as `POST /chat/stream`; `/chat` is unchanged. In a
real browser through the proxy: first words at 11.3 s and finished at 19.9 s
with the model not yet loaded; first byte at 0.5 s and finished at 6.3 s with
it warm. Only the two Ollama providers stream; a cloud answer still arrives
whole. During the live check free memory went to 17% once, under the agreed
20% floor, with the model, the API, the dashboard and a headless browser all
running; the guard unloaded the model.

### S2. Record prompt-read and answer-write time per call (in review, #129)

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

**What happened.** Kept in `llm-timings.jsonl` beside the audit trail, not in
it, because the daily quota counts the trail's lines. The check asked for
above changed the plan. On Ollama 0.40.0:

- the prompt count is the whole prompt even when most of it was reused;
- a prompt longer than the context is **refused** (HTTP 400), not cut. The
  provider used to report only "400 Bad Request"; it now quotes the daemon's
  reason with both numbers;
- the silent case is a prompt that fits with an answer that does not (3,796
  read, 600 written, context 4,096, an ordinary reply). That is what
  `context_full` flags.

### S3. Try an 8-bit KV cache to double the local context (dropped, #130 records it)

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

**What happened.** Measured on a second daemon so the owner's settings were
not touched. The first half of the bar held and the second did not.

| cache, context | held | all 12 | writes |
|---|---|---|---|
| 16-bit, 4,096 (shipped) | 4.82 GB | 146 s | 11.7 tok/s |
| 8-bit, 4,096 | 4.54 GB | 134 s | 13.5 tok/s |
| 8-bit, 8,192 | 4.98 GB | 192 s | 8.7 tok/s |

The 8-bit answers were the 16-bit ones word for word on 7 of 12 and differed
in wording only on the rest. `OLLAMA_NUM_CTX` stays 4,096. #130 puts the table
in CLAUDE.md §14 and the how-to in `docs/USAGE.md`. See S6 for what is left.

### S4. Make Claude Code ask before it approves an application (in review, #126)

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

**What happened.** The check asked for above found something stronger than the
rule. An MCP server can mark a tool with
`_meta["anthropic/requiresUserInteraction"]`, and Claude Code 2.1.214 and
later then prompts on every call in every permission mode, past any allow
rule. Both are in: the mark on the two tools, and the ask rule in a new shared
`.claude/settings.json` for a build that does not read the mark. An ask rule
does prompt in auto and bypass modes, by the docs. Not watched live: the
Job Runner MCP server is switched off in this machine's
`.claude/settings.local.json`.

---

## Open, from the work above

### S5. Record a streamed answer's timings (in review)

#128 adds `OllamaProvider.stream` and #129 records `complete` and
`complete_json`. They were written apart, so a streamed answer, which is now
every answer in the dock, is not in `llm-timings.jsonl`. The closing line of
Ollama's stream carries the same numbers. A few lines in `stream`, once both
have merged. The daemon's refusal reason (`_ollama_reason`) needs the same
wiring there.

One more for the same sitting. For a prompt too long for the local model,
`/chat` now shows the right reason inside the wrong advice: "The local model
is not answering (the prompt is 6,076 tokens and the model's context is
4,096 ...). Start Ollama with `ollama serve`, or pull the configured model".
The advice is `did_not_answer` in `chat.py`, which #128 moves, so it was left
out of #129. It should say what to do about a long prompt when that is the
reason.

**What happened.** #128 and #129 merged within a minute of each other, so all
three were done in one pull request off `main`, with the review fixes that
had been pushed to those two branches after they merged. The stream records
its closing line's numbers and reads a refused body before raising; a prompt
too long for the model is its own error, `PromptTooLong`, with its own advice.

### S6. Decide on the 8-bit cache for your Ollama (open, the owner's call)

At the shipped context it held 0.28 GB less and gave the same answers. It is a
setting of the Ollama app, not of the repository, so it was not applied.
`docs/USAGE.md` in #130 has the two `launchctl setenv` lines. They last until
the Mac restarts, and they were measured through `ollama serve`, not through
the app.

### S7. A timing test that fails on a slow CI runner (open)

`tests/test_shared_ratelimit.py::test_four_concurrent_fetchers_are_spaced_by_the_floor`
failed once on #126, in the gate-5 step, after passing in gate-0 of the same
run: requests 1.886 s apart against a 2.0 s floor with 5% allowed. #126 does
not touch the crawler. The job was re-run. The allowance, or the clock the
test reads, is what to look at.

### S8. The tracker's upcoming list leaves out a task with no date (open)

Found while building M3. `/tracker` asks for tasks due within 14 days, and a
task with no date is not in that list. The interview or assessment task a
recruiter's reply creates has no date until the owner sets it, so it is
missing from the one list meant to show what is coming. It does show on the
application's own page.

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

---

## MCP: where else an assistant could reach

Added 2026-10-07. The MCP server reached applications, résumés, projects,
profiles and the posting search. The API has more than that. In the order
they were proposed:

### M1. The owner types the missing answers and the code (merged, #132)

`approve_application` took the answers as an argument and `submit_otp` took
the code, so a model typed what went on the employer's form. Both are now
asked for in a form the client shows the person. Neither is an argument any
more.

### M2. The matches feed (merged, #133)

`my_matches`: the scored feed with the matches page's filters. Read only.

### M3. Tracker and follow-ups (merged, #134)

"What needs a follow-up this week?" The tracking API has 11 routes and no
tool. Start read-only.

**What happened.** Two tools, both read only. `follow_ups` lists open tasks
(overdue, due in the next week, undated, and a count of later ones) and the
submitted applications no employer has answered. `application_tracking` is
one application's tasks and contacts. A contact is sent as name, relationship,
company and role; their email, phone, profile link and the owner's notes on
them are not. Adding or changing a task over MCP was left out, and is the
next step if it is wanted.

Found on the way and fixed in the same pull request: an id given to any tool
went bare into the path it asked the API for, so `../inbox?` in place of an
application's id read the recruiter mail, and `../resumes/<id>/edit?` sent the
guarded résumé edit to the route that does not guard. Every id is checked now.
`CLAUDE.md` §9, Phase 4, has what was measured.

### M4. Status questions (in review)

The audit trail ("what left my machine this week"), setup health, the weekly
digest, starting a crawl. Read-only except the crawl.

**What happened.** Five tools. `audit_trail`, `setup_health`, `weekly_digest`
and `crawl_status` read. `start_crawl` queues one crawl and is the third tool
that makes Claude Code ask you on every call. Two things the API did not have
were added for them: the audit summary takes a number of days and counts
uploads by task, and `POST /crawl` starts a crawl through the same
`request_crawl` that `make crawl` and the assistant's "run crawler" use.

Left out on purpose: the setup page's registry repair, and the audit page's
check of a pasted text against the trail. The first rewrites company rows.
The second would mean handing the assistant the text to be checked.

### M5. Grading postings by conversation (open)

The labeling loop needs 100 or more of the owner's grades and has none. The
grade has to be typed by the owner, with the form M1 uses, and never chosen
by the model, or the benchmark grades itself.

### Set aside, to think about later

The owner's words on 2026-10-07: "later we will think about" these. They were
listed as places not to use MCP, with the reason for each.

- **Recruiter mail text.** Over MCP the assistant is a remote model, so the
  mail would leave the machine; §14 gates that.
- **Finishing captcha-blocked forms with a browser MCP.** §2.5 says the owner
  finishes those by hand.
- **Data API Builder's MCP endpoint.** It would be anonymous, so it stays off.
- **Outside connectors such as Gmail for the Gate 6 emails.** `make
  import-mail` does the same job without the mail passing through a remote
  model.
