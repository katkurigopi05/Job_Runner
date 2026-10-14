# Notes on the LLM providers

Moved here from the root `CLAUDE.md` on 2026-10-11, word for word, so they
load only when work touches this directory. "§N" below is a section of
the root file, which keeps every heading and says where each section went.

## §7: LLM provider abstraction

Implementations: `OllamaProvider`, `OllamaCloudProvider`, `GeminiProvider`,
`AnthropicProvider`, `StubProvider`.
Selected by `LLM_PROVIDER` env var. `StubProvider` returns deterministic canned output and
is what tests use — no test may hit a network LLM.

Task routing (`packages/llm/router.py`):

| Task | Default provider | Temp | Why |
|---|---|---|---|
| classify inbound email | Ollama | 0.0 | cheap, local, easy; one word from a fixed set |
| map form field to profile key | Ollama | 0.0 | structured, low creativity |
| tailor résumé | best available | 0.3 | quality matters most here |
| write cover letter | best available | 0.7 | the one task where variance buys something |
| answer open-ended question | best available | 0.3 | goes on a real application |

Temperature is routed per task for the same reason the provider is — these
tasks want opposite things from a model. Left unset they run at each vendor's
default, around 1.0, which is wrong for nearly all of them. Tailoring in
particular is bounded by the fabrication guard: a creative model there does
not write better résumés, it raises the rejection rate and falls back to the
original bullet, which looks like the tailorer doing nothing.

The table lives in `packages/llm/router.py::TEMPERATURES`. `complete_json` is
pinned to 0.0 regardless — the answer has to parse against a schema.

**Local or cloud is now the owner's choice, per task.** The provider column
above was not settable: `best_available()` walks a hardcoded `QUALITY_ORDER`
and picks the strongest *configured* provider, so a Gemini key made every
"best available" task remote and the only way to tailor locally was to delete
the key that everything else wanted kept. `LLM_TASK_TAILOR`,
`LLM_TASK_COVER_LETTER` and `LLM_TASK_OPEN_ENDED` take `auto` (the shipped
behaviour) or a provider name.

Only those three. Classification and the assistant read recruiter mail and
chat context; §2.8 permits one third-party upload and neither is it, so both
stay on Ollama in code and `CHOOSABLE_TASKS` does not list them — a setting
able to move them would be a way to opt out of a non-negotiable by editing
`.env`. `test_only_the_uploading_tasks_are_choosable` holds that.

The assistant has since gained a cloud option, and it is worth being precise
about what that did and did not change here. It is **still absent from
`CHOOSABLE_TASKS`**, and `/chat` still ignores `LLM_PROVIDER`. The owner picks a
provider per question in the UI (§14); no environment variable redirects the
assistant, which is the property this list protects. Inbound-email
classification did not move at all.

**OpenRouter is a fourth provider, and it is opt-in by name.** One key reaches
many upstream models, and `docs/PARITY.md` had it refused under §3's "no paid
service without asking" — the owner asked, and the route in use is free, so §11
is untouched. What is *not* untouched is §2.8. OpenRouter forwards the résumé
text to an upstream provider, and the audit trail can record the hop but not the
destination; on a cloaked `stealth/*` route the upstream vendor is undisclosed
by design, and free routes commonly log prompts and share them with that
undisclosed creator. So `OpenRouterProvider` is deliberately absent from
`router.QUALITY_ORDER`: a key in `.env` changes nothing on its own, and the
provider answers only when named by `LLM_PROVIDER` or one of the three settings
above. Acquiring a route whose recipient cannot be named should take typing the
word, not pasting a key.

The endpoint has one property worth knowing before pointing anything at it:
**reasoning is mandatory and cannot be switched off.** Both `{"enabled": false}`
and `{"max_tokens": 0}` come back `400 "Reasoning is mandatory for this endpoint
and cannot be disabled"`. That matters because `max_tokens` there bounds
reasoning *plus* answer, while every caller here means it as an answer budget —
`tailor_bullets` passes 300 to keep a bullet bullet-sized. Passed through
unchanged, a 300-token call returned `finish_reason="length"` with **empty
content**: the allowance went on thinking and the model was cut off before
writing. The tailorer caught the error and kept the original line, so the
symptom was a tailorer that appeared to do nothing on a provider that was
working fine. `REASONING_HEADROOM_TOKENS` is the fix, and the numbers behind it
are in the constant's docstring.

Free routes also rate-limit hard: a three-bullet résumé trips 429 at the default
`LLM_CALL_INTERVAL_S=4.0`. The pacer obeys `Retry-After` and retries, but for a
real batch raise the interval.

`LLM_FALLBACK_LOCAL` answers with the local model when the daily allowance is
spent or the remote provider is unreachable, rather than refusing. This is not
a softening of "nothing falls back to the stub" — that stands, and the stub is
excluded explicitly. `QuotaExceeded` already told the owner to "raise the
limit, wait for the reset, or run a local provider"; the third option was an
instruction to a human, and this is it automated. Which model answered is
recorded on the provider and in the trail, because a résumé tailored by
llama3.1 after the allowance ran out is a different document from one tailored
by Gemini and the owner approving it should be able to tell.

"Should be able to tell" was, until now, only true of someone reading the
trail. `answered_by` lived on the provider object and died with the run, so the
review screen — the one place the distinction has consequences, because it is
where the document gets approved and sent — never showed it. It is now stored
on the résumé as `resumes.tailored_by` and rendered under the diff.

On the résumé rather than the application, because the reuse paths are the ones
that would otherwise go blank: an overnight batch and the tailoring cache both
serve a document written in an earlier run to an application that never calls a
provider, and the model is a property of the document, not of the run that
attached it. Read *after* the rewrites too, not before — `FallbackProvider`
resets `answered_by` to the primary at the top of every call, so a value
captured early names the model that did not answer, which is precisely the case
worth seeing.

NULL means unrecorded — a base résumé, or one tailored before the column
existed — and the screen says "not recorded" rather than showing nothing. A
blank line reads as "no fallback happened", which is the one thing it must not
be mistaken for. The migration deliberately does not backfill: there is no
record of which model wrote the existing rows, and a plausible guess on a
document about to be sent to an employer is worse than an honest gap.

The cover letter records the same thing, in the review record rather than on a
row. §7's fallback applies to `write_cover_letter` exactly as it does to
tailoring, so a letter written by llama3.1 after the allowance ran out is not
the letter Gemini would have written, and it goes to an employer under the
owner's name. A tailored résumé has a row to carry `tailored_by`; a letter has
none, so `answered_by` rides in `review_json["cover_letter"]` beside the text.
Refusals carry it too — "the guard refused it" reads differently depending on
which model produced the draft.

The review screen shows the letter itself, open by default. It had been written
into `review_json` since the wiring landed and rendered nowhere, so a document
generated by a model was attached to applications the owner approved without
ever being able to read it — the same failure the untailored résumé had, and
invisible for the same reason: no test asserted what the approval screen
displays.

## §14: the assistant's local model

The model is **llama3.1**, chosen by benchmarking the six local models against
this project's own tasks rather than by reputation. On the 30 labeled recruiter
emails behind Gate 6 it classified 30/30 where the next best managed 28; it
answers the assistant's probes from the context it was handed; and asked what
salary to request it points at the profile, where `qwen2.5-coder` instead
advised on how to research one — the §2.2 failure, from the model rather than
the route. It is also the tersest of the six, and `CHAT_SYSTEM` asks for brief.

Worth knowing what that benchmark did *not* prove. Of §7's five tasks only
`tailor_resume` has a caller; `classify_inbound_email`, `map_form_field`,
`write_cover_letter`, and `answer_open_ended_question` are defined in
`packages/llm/router.py` and called from nowhere, and `LLMClassifier` is never
constructed — the inbox is rules-only, deciding 29 of those 30 emails without
a model at all. The assistant is the one live local-model path, so it is the
one the choice was made on.

**It is Qwen3 8B at 3 bits since 2026-10-06**
(`hf.co/unsloth/Qwen3-8B-GGUF:Q3_K_M`, 4.1 GB), and the two paragraphs above
describe the model it replaced. The owner asked for a better local model and
it was measured on the assistant itself: twelve questions (the four prompt
buttons, Kafka, "any jobs role based on ai", security, Zig, a paraphrase, a
date that is not in the data, "hello", "jobs at Stripe"), each prompt built
from the owner's data the way `/chat` builds it, each answer checked against
what the model had been handed. Same request as the provider sends, context
capped at 4,096, thinking off, one model loaded at a time.

| | right | partly | missed | wrong or invented | all 12 | held | lowest free |
|---|---|---|---|---|---|---|---|
| `llama3.1` (Ollama, 4-bit) | 2 | 5 | 0 | 5 | 102 s | 5.26 GB | 32% |
| Qwen3 8B, Ollama `Q3_K_M` | 9 | 2 | 1 | 0 | 141 s | 4.82 GB | 34% |
| Qwen3 8B, MLX 3-bit | 7 | 2 | 0 | 3 | 181 s | 4.70 GB | 36% |
| Qwen3 8B, MLX 4-bit DWQ | 8 | 3 | 1 | 0 | 146 s | 5.69 GB | 31% |
| Qwen3 8B, MLX 4-bit | 9 | 1 | 1 | 1 | 143 s | 5.69 GB | 30% |

`llama3.1`'s five: "You have 3 profiles that need manual completion" (they are
applications, and the one awaiting review was left out); nine kinds of
security role it made up after the real ones; "the interview date is copied
word for word from your profile"; "We haven't started searching for jobs"; and
"all four postings are from Stripe" under a list of five.

Four things to keep in mind about that table:

- **One run each, twelve questions, one judge.** A one-answer difference is
  noise. The gap between 5 wrong and 0 is not.
- **MLX was tried as the engine and not adopted.** It writes at 17 to 21
  tokens a second against Ollama's 12, but reads the prompt at about 150 a
  second, so a 1,300-token prompt waits 8 s for its first word against about
  5 s. End to end it was no faster, no more accurate, and held the same at the
  same bits. It would also need an idle unload built, which Ollama does itself.
- **The 4-bit build of the same model does not fit.** Loading a 5 GB model
  takes about 1.5 times its size for a few seconds: `llama3.1` took the
  machine from 71% free to 26%, and `qwen3:8b` straight after it to 20%.
- **Two things made the result and both are now in the request**
  (`OllamaProvider`, `tests/test_ollama_local_model.py`). Qwen3 reasons before
  it answers and every caller passes `max_tokens` as an answer budget, §7's
  OpenRouter trap again, so `think: false` is sent to a model the daemon lists
  as thinking. And the context is capped by `OLLAMA_NUM_CTX` (4,096), because
  what a loaded model holds grows with it. A hosted `:cloud` model is sent
  neither.

What was *not* measured: tailoring and the cover letter. The local model also
answers those when `LLM_TASK_TAILOR=ollama` or `LLM_FALLBACK_LOCAL` sends them
there, and in the local column of **Compare models**. Qwen3 at 3 bits has not
been run on them. `llama3.1` and the 4-bit `qwen3:8b` were deleted from the
owner's machine the same day, at their request.

**That paragraph used to end** "a prompt longer than the context cap is cut by
Ollama without an error". Nobody had sent one. Sent on 2026-10-07 to Ollama
0.40.0, a 6,076-token prompt at a context of 4,096 was refused: HTTP 400,
"request (6076 tokens) exceeds the available context size (4096 tokens)". What
is silent is the other case, a prompt that fits with an answer that does not:
3,796 tokens read and 600 written came back as an ordinary reply.

**A smaller cache and a larger context, tried and not adopted** (2026-10-07).
Ollama can keep its attention cache at 8 bits (`OLLAMA_KV_CACHE_TYPE=q8_0`,
which needs `OLLAMA_FLASH_ATTENTION=1`). The cache grows in step with the
context, so the question was whether 8,192 tokens could be had for what 4,096
costs. Run on a second daemon on its own port, so the owner's settings were
not touched; the twelve prompts saved from the comparison above, the same
request, one load at a time.

| cache, context | held | lowest free | all 12 | writes | reads a 1,300-token prompt | same words as the first row |
|---|---|---|---|---|---|---|
| 16-bit, 4,096 (shipped) | 4.82 GB | 22% | 146 s | 11.7 tok/s | 5.9 s | |
| 8-bit, 4,096 | 4.54 GB | 28% | 134 s | 13.5 tok/s | 5.9 s | 7 of 12 |
| 8-bit, 8,192 | 4.98 GB | 22% | 192 s | 8.7 tok/s | 9.3 s | 6 of 12 |

- **The 8-bit cache did not change the answers.** Seven of twelve are word for
  word the 16-bit ones, and the five that differ were read against their
  prompts and differ in wording only. Two 16-bit runs a day apart differ on
  four. At 8,192, eleven of twelve are word for word the 8-bit answers at
  4,096.
- **It saves 0.28 GB at the same context.** The daemon's log gives the cache
  as 306 MiB against 612.
- **8,192 was not free.** 0.16 GB more than is held today, and about a third
  slower at reading and at writing. Why it is slower was not established.
- **It does what it is for.** At 8,192 the 6,076-token prompt was read whole
  and answered correctly, in 84 s, 78 of them reading.

`OLLAMA_NUM_CTX` stays 4,096. The assistant's longest prompt is about 1,400
tokens with 600 to answer, so nothing it sends needs the room. The case for
more is tailoring and the cover letter on the local model, which is still
unmeasured, and a prompt too long for it is refused, not cut. The 8-bit cache
at 4,096 is a setting of the owner's Ollama and not of this repository:
`docs/USAGE.md` says how, and it was not applied to their daemon.

One run each. Tests and type checks were running on the machine during all
three, so the times are loose; what was held and what was answered are not.

Found on the way and not fixed: with no recruiter replies stored the context
has no replies section at all, so no model can answer "Any replies?" with
"none". Qwen restated the application counts; `llama3.1` said "no new replies",
which was true and was a guess.

## §14: what a call cost, and Ollama's hosted models

**What a call cost is kept beside it** (2026-10-07). Ollama answers every chat
call with the tokens it read and wrote and how long each took, and the
provider kept only the text. `packages/llm/timing.py` writes them to
`llm-timings.jsonl`: counts, durations and the digest the trail holds for the
same call. It is a second file because `quota.py` counts the trail's lines,
one per call. Three things were measured on Ollama 0.40.0 that day, with the
local model at a context of 4,096:

- **The prompt count is the whole prompt** even when most of it was reused
  from the question before: 560 tokens reported, read in 0.25 s, against
  4.2 s for the first question with the same opening.
- **A prompt longer than the context is refused, not cut.** HTTP 400, with a
  body naming both numbers ("request (6076 tokens) exceeds the available
  context size (4096 tokens)"). The provider used to report "400 Bad Request"
  and nothing else, so it read as a model that was down;
  `provider._ollama_reason` now quotes the daemon, for this and for any other
  refusal. `tests/test_ollama_refusal_reason.py` uses the body it sent.
- **An answer that runs out of context is silent.** A prompt of 3,796 tokens
  with the usual 600 to answer came back as 600 written and
  `done_reason: "length"`, 4,396 tokens through a context of 4,096. The
  daemon made room by dropping the start of the prompt and said nothing.
  `CallTiming.context_full` is that sum reaching the cap, logged as
  `llm_context_full`.

Not in it: any provider other than the two Ollama ones. Nothing reads the
file yet except `timing.read_timings`.

**This used to say a streamed answer was not in it either**, and once the
stream (§17) had merged that was every answer in the dock. The two were
written as separate changes, so with both on `main` the assistant's calls
were the one kind that kept no numbers, and a refusal over the stream read
"400 Bad Request" again: a streamed body is not read until it is asked for,
and the reason is in the body. The closing line of Ollama's stream
carries the same measurements as a plain reply, so `OllamaProvider.stream`
records them there, and reads a refused body before raising. A stream closed
early records nothing; the numbers are on the line it never read.

A prompt too long for the model is raised as `PromptTooLong`, so `/chat` can
tell it from a model that is down. It had been showing the right reason
inside the advice for the wrong one ("Start Ollama with `ollama serve`").

---

### Ollama's own hosted models

The owner asked for `glm-5.3-flash:cloud`. Recording how it landed, because the
obvious implementation was the wrong one and the difference is the whole point.

The obvious one is to let `OLLAMA_MODEL` take a `:cloud` tag. That is precisely
what the refusal above exists to stop: Ollama serves hosted models through the
identical `localhost:11434` API, so nothing in the request distinguishes them,
and a remote answer would carry the label `provider="ollama", local=true`.
Loosening the check would not have widened a choice, it would have removed the
owner's ability to know which they got.

So the remote path got **its own provider name**, `ollama_cloud`, and the
refusal is untouched — `test_asking_for_local_still_refuses_the_same_model`
runs the same model through both and asserts one answers and one is refused.
Naming it is the informed decision §14 was protecting; `OLLAMA_MODEL` still
means "a model on this machine".

Two consequences worth keeping visible:

- **`audit.is_local` is now right by construction rather than by substring.**
  Under `ollama` a call is presumed local and only "cloud" in the tag rescues
  the label; under `ollama_cloud` it is presumed remote. A retag cannot make
  the trail lie.
- **The provider refuses a *local* model**, which sounds like pedantry and is
  not. §2.8's trail is worth having only if it is read, and one that reports a
  résumé leaving when it did not teaches the owner to stop reading it. Both
  directions of mislabelling are refused, for one reason.

**It is deliberately absent from `router.QUALITY_ORDER`, and that matters more
here than it does for OpenRouter.** Every other remote provider needs an API
key, so an absent key keeps it out of "auto" on its own. This one needs no
credential once the local daemon is signed in — `_configured` is satisfied by
`OLLAMA_CLOUD_MODEL` naming a model. Were it in the quality order, that single
line in `.env` would send every "auto" task off the machine, with nothing in
the configuration that reads as having asked for it. §2.8 permits the upload;
it does not permit it happening unnoticed.

It is **not** in `CHOOSABLE_TASKS` either, so nothing about the list above
moved: the assistant is still chosen per question in the UI, never by an
environment variable.

Unlike an OpenRouter `stealth/*` route, the recipient is nameable — Ollama's
servers, running the tagged model — so the trail records where the résumé went
rather than only that it left. Both UI pickers say so at the moment of
choosing, and both say the quiet part: the address is localhost either way.

## §7: Gemini's answer budget, and an answer that was cut off

Added 2026-10-13, after the notes above were moved here. Found by tailoring
one résumé for one posting and reading why so little of it changed.

Gemini's flash models think before they write, and `maxOutputTokens` bounds
the thinking and the answer together. It is the OpenRouter trap above, on the
provider the project ships with, and worse in one way: the cut-off answer
comes back as a 200 **with text**. Measured on `gemini-3.8-flash` with the
tailorer's prompt and a fixture bullet, the 300-token cap left 8 tokens of
answer after 288 of thinking, and `complete` returned that fragment. With
room, six samples thought 839 to 1,498 tokens and wrote 15 to 40.

The fabrication guard does not catch a fragment: half a bullet claims nothing
the résumé does not. The owner's storage held the proof. Résumé v5, tailored
by Gemini on 2026-09-02, has one bullet that is the first 8 of its source's
17 words. It is attached to no match and no application.

Two things to keep (`tests/test_gemini_cut_off.py`):

- **`THINKING_HEADROOM_TOKENS` (4,096) is added to every request.** The
  caller's `max_tokens` still means the answer.
- **`finishReason: MAX_TOKENS` is refused whatever text came with it**, and
  the text is not quoted in the error. This is the half that holds if the
  headroom is ever too small.

Not measured: `gemini-3.6-flash`, the pinned model, which answered 503 or 429
to every call that night. Not changed: a 503 is not retried; the
OpenAI-compatible providers still return partial content when `finish_reason`
is `"length"` and the content is not empty; `complete_json` sets no limit and
reads no `finishReason`.
