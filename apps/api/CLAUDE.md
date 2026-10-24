# Notes on the API

Moved here from the root `CLAUDE.md` on 2026-10-11, word for word, so they
load only when work touches this directory. "§N" below is a section of
the root file, which keeps every heading and says where each section went.

- The approval gate's row lock (§27) is in `packages/core/CLAUDE.md`.
- The assistant's search is in `.claude/rules/assistant-search.md`, and its local model in `packages/llm/CLAUDE.md`.

## §14: the assistant

**Kaggle GPU is an explicit remote choice.** `provider="kaggle"` uses the
loaded Qwen3 8B model in the owner's running Kaggle notebook through its
authenticated Jupyter connection. The default stays Ollama. The existing
mail/history gate and protected-topic refusal apply unchanged. Session URLs
contain credentials in their path and stay in an owner-private file under
`storage/`, never in API errors or transport logs. Setup and session lifetime:
`docs/KAGGLE_ASSISTANT.md`. No prompt change is needed for this transport.

It was built after §9's phases and is not one of them. Three properties are
load-bearing and easy to undo by accident.

**Local by default; remote only when asked, per question.** This paragraph used
to read "local-only, not configurable", and that was the rule for good reason:
chat context carries application URLs, profile fields, and recruiter
correspondence, and §2.8 permits exactly one third-party upload — the tailoring
call — which a chat window is not.

**The owner widened it deliberately.** `ChatRequest.provider` accepts `ollama`,
`gemini`, `anthropic`, `openrouter` or `ollama_cloud`, and the `/chat` UI has a
picker. Naming a
remote provider sends the context to it. Recording the change rather than
quietly editing the rule away: this *is* a narrowing of §2.8, it was made
knowingly, and a reader who finds recruiter mail in a Gemini log should be able
to find out why here.

What did not change is what happens when nobody asks:

- Omitting `provider` answers locally. The shipped default still costs nothing.
- `apps/api/routers/chat.py` still ignores `LLM_PROVIDER` entirely, so nothing
  about tailoring configuration moves the assistant.
- The assistant is still **not** in `CHOOSABLE_TASKS`. The choice is a
  per-request field made in the UI for one question, not an environment
  variable that silently redirects every future conversation — the `.env`
  opt-out route §7 warns about stays closed.
- **No fallback in either direction.** A local model that is down does not get
  promoted to a cloud provider, and a cloud provider that fails does not drop
  to the local one. `LLM_FALLBACK_LOCAL` covers tailoring and deliberately does
  not reach here: there the fallback is recorded on the document, here it would
  be a different answer wearing the same label.
- **Inbound-email classification is untouched** and remains local in code. The
  owner widened the assistant, not the inbox.

The `audit.is_local` check survives, narrowed to the local path. Ollama serves
cloud-hosted models over the same localhost API — `kimi-k2.6:cloud` and
`qwen3-coder:480b-cloud` are both in the owner's model list, neither runs on
this machine, and the base URL is identical either way. That check was never
only about distance; it is about the label matching. Choosing Gemini openly is a
decision. Asking for the local model and silently getting a third party is not a
decision at all, so it still refuses.

The reply carries `model` and `local`, both computed rather than inferred from
the provider name, and the UI marks every remote turn. An answer that cost
privacy must never look like one that did not.

**Recruiter mail is gated separately, and defaults to withheld.** Choosing a
cloud provider does not take the inbox with it. `ChatRequest.share_mail` is
`False` unless the owner ticks the box for that question, and the `/chat` panel
shows the box only when a remote model is selected.

It is separate from the provider choice because it is a different decision. The
rest of the context is the owner's own material — their applications, their
profile, and, for a question about skill gaps, the skill names read from their
résumés (never the résumé text; §20). Recruiter correspondence is *other people's writing about them*, sent
privately, by people who never chose a provider. Consenting to send your own
data somewhere is not the same as consenting to send theirs, so the two are not
one switch.

For the local model it is always included and the toggle is not shown. The gate
is about crossing a boundary; on the side where nothing crosses there is nothing
to gate, and rendering a control that does nothing would be worse than
rendering none.

When it is withheld the context says so — `recent replies: withheld` — rather
than omitting the section. The assistant is told to answer from what it was
handed and to say when it does not know, so a missing section would read as "no
replies have arrived", which is a different answer and a wrong one.

## §14: the assistant, continued

**§2.2 is refused before the model is reached.** Asked what to put for work
authorization, sponsorship, employment history, or salary, the route returns a
refusal and points at the profile. The system prompt says the same thing, but
the prompt is a request and this is a rule — the check runs in code.

**Grounded, not freehand.** The model is handed real counts, the actual
application, and its recent replies, and told to say when it does not know. An
assistant that invents an application status is worse than no assistant.

**An employer's applications have their own section** (2026-10-21).
`prepare` uses retrieval's shared first-person application-status rule and
`found.companies` to supply `YOUR APPLICATIONS AT …`: the code counts and lists
each application's status, failure reason and dates, or explicitly says none.
A linked posting identifies the company; only an unlinked application may
fall back to an exact ATS vendor/registry board slug from its URL. A jobs
question gets no application section. `assistant.system` v7 distinguishes
these company facts from the global status counts: v6 could attribute global
application totals to a named employer.

**"Run crawler" is a command, and commands are handled in code.** Saying "run
crawler", "find new jobs", "refresh the postings" and the like queues a crawl
through `crawl_job.request_crawl` — the same door `make crawl` uses, with its
one-crawl-at-a-time guard — and replies without asking any model, whichever
provider is selected. The match is anchored to an imperative at the start, so
"Which new jobs mention Kafka?" is still searched and "Did the crawler run?" is
still answered. The reply says whether a worker is alive, because a crawl
queued with nobody to run it reads exactly like a registry with nothing new.
This reverses one half of a recorded decision: `routers/crawl.py` said starting
a crawl "stays a deliberate act at a terminal". The owner asked for it on
2026-10-04; a typed command is still deliberate, and the API is still
loopback-only.

The audit trail in `packages/llm/audit.py` records every provider call —
digests and sizes, never the prompt itself. §2.8 wants proof of what left the
machine; §10 forbids logging résumé contents. Both hold: the trail proves what
was sent without becoming a second copy of it.

### Remembering a conversation

Added 2026-10-08, at the owner's asking. `/chat` took one message, so "which
of those are remote?" referred to nothing. The dock now sends the last few
exchanges with each question (`ChatRequest.history`), and
`apps/api/chat_history.py` decides what of them a model sees. Three rules in
this section were written for one message at a time, and each had a way round
it once there were two. `tests/test_chat_memory.py` holds them.

- **Recruiter mail.** The local model always reads it, so its answer can
  quote it. Sent on as history, that is the mail reaching a remote model with
  the box unticked. An exchange written with mail in its context goes to a
  remote model only when the owner shares mail for that question. The reply
  says whether mail was read into it (`mail_in_context`), which is narrower
  than `shared_mail`: mail is only read for an application, so a local answer
  on the chat page holds none. An exchange that does not say is treated as
  holding it.
- **§2.2.** "What salary does this posting list?" is answered, and "what
  should I ask for?" names no protected topic. Together they are the question
  the rule refuses. A first-person question is asked without any earlier
  exchange that touched one of those topics, and a question the rule refuses
  outright is never remembered. "Which roles offer sponsorship?" followed by
  "which of those are in California?" still works: the second is not about
  the owner.
- **Grounded, not freehand.** An earlier answer is the model's own prose. The
  postings it cited are read again from the database and given as `POSTINGS
  FROM EARLIER ANSWERS`, labelled after the ones found for this question.
  Citation labels are taken out of the remembered text, because `[P1]` named
  another posting then. A carried-over posting is listed under an answer only
  when the answer cites it.

Whatever is held back is said in the prompt, as withheld mail is: an absent
exchange would read as nothing having been said.

Three exchanges are given, a question cut to 300 characters and an answer to
500, about 600 tokens against the local model's 4,096. The system prompt is
`assistant.system` v6 (it was v5 until §27's paragraphs joined these in it). The protected-topic checks moved to
`apps/api/protected_questions.py`, so the refusal and the memory read a
question the same way; `routers/chat.py` was at 800 lines.

Seen on 2026-10-08:

- **In a browser**, with the model's answer stubbed at the network. The
  second question carried the first exchange and the posting it cited, a
  refused question was not sent as history, and "New conversation" cleared
  it.
- **Through the API on the owner's data**, Nemotron on OpenRouter, two calls.
  "Which open roles use Kafka?" listed five postings. "which of those are
  remote?" answered that only the Senior Software Engineer, Data Platform
  role at Human Interest is, and cited it by the label it had in that
  message. It is the one of the five located "United States, Remote".

**A question can be edited and asked again** (2026-10-12, at the owner's
asking; `apps/web/src/components/assistant-turn-actions.tsx`). The owner had
typed one question three ways to get round a wrong answer, and each retyping
went back to the model as history, so the third answer repeated the first.
An edited question is sent with only the exchanges before it, and the turns
below it leave the screen, because what is on screen is what is remembered.
The editor says how many turns that removes. Any turn can be copied. Only a
question can be edited: an edited answer would be words put in the model's
mouth and sent back as history. `tests/test_assistant_copy_edit.py` holds the
wiring; the behaviour was watched in a browser with the model stubbed, on
`/chat` and in the dock, where Escape closes the editor and not the dock.

Not done:

- **The memory is the open page's.** The conversation is in the dock's state
  and is gone on a reload. Nothing is stored, in the browser or the database,
  so nothing is remembered from one day to the next.
- **A follow-up is still searched as it is written.** "which of those are
  remote?" is not a postings question to the search, so it was answered from
  the carried-over postings alone. "which of those use Python?" also runs a
  fresh search for Python, and what that finds is listed beside them.
- **The local model was not asked.** One run, one model.
- **The two-message rule reads topics from the refusal's own word list**, so
  it misses what that misses.

## §17: live status updates, and the process boundary they had to cross

`GET /events/applications` (`apps/api/routers/events.py`) streams application
status changes to the dashboard, which was `force-dynamic` and therefore only
as current as the last reload. The mechanics are in
`apps/api/status_stream.py`; the rest of this section is the part a later
change could quietly break.

**The events come from the log, not from the code that changes a status**, and
that is the whole design rather than an implementation detail. `transition()`
has three callers:

```text
apps/api/routers/applications.py   this process — approve, reject, OTP, manual submit
apps/worker/apply_job.py           the worker process
packages/inbox/route.py            the worker process
```

`make api` and `make worker` are separate processes. An in-process publisher —
the obvious build, and the one that demos perfectly — carries the owner's own
clicks and **nothing the machine does**: no parking at `needs_review`, no
failed run, no OTP request. Those are the events the dashboard exists to show,
and they are all written by the other process.

So the watcher tails `application_events`, which `transition()` writes in the
same transaction as the status change (§6), plus the `created` row the API
writes beside a new application. Three consequences worth keeping:

- The worker needs no change and cannot forget to publish.
- The stream cannot disagree with the database. A publish that happened beside
  a transaction that rolled back is not a state this can reach.
- There is one publication path, so an API-originated change cannot arrive
  twice.

`_announce()` in the applications router is **latency, not delivery**: it wakes
the watcher so a click does not wait for the next tick, and the watcher still
reads the committed row itself. Deleting every call to it costs a second and
loses nothing. It is called *after* the commit for the reason §15 records the
doorbell learning — announcing inside the transaction announces a change that
may still roll back — and `test_the_stream_is_only_ever_woken_after_a_commit`
reads the source to hold that ordering, because no runtime assertion can see
the difference on a transaction that happens to commit.

**Postgres `LISTEN/NOTIFY` is the obvious upgrade and was not taken.** It is
not an external service and would cut the latency to nothing, but it needs a
dedicated raw connection and a reconnect loop for a single local reader. A
one-second tick on one user's dashboard is not a problem anyone has. Redis and
Kafka are refused outright (§11).

Four properties that are easy to undo:

- **Every event carries the whole counts map.** The client assigns a snapshot
  rather than applying a delta, so a missed frame costs one update's staleness
  instead of permanent drift that nothing on the page could detect.
- **Every status is present in that map, including the zeroes.** A key that
  appears only when non-zero is exactly how `/tracker` dropped five of seven
  columns (§15).
- **Nothing polls while nobody is looking.** The watcher starts on the first
  subscriber and is cancelled by the last unsubscribe, so this feature costs
  an owner who never opens the dashboard nothing at all.
- **The stream is never the source of truth.** If it never connects, or drops
  and cannot return, the page shows the server render — what it did before any
  of this existed. The fallback is the old behaviour, not a degraded mode.

**The cursor is deliberately sloppy, and has to be.** `ApplicationEvent.at` is
`func.now()`, the *transaction* clock — the same fact §15 records breaking a
notification test. Events in one transaction share a timestamp, and a long
transaction commits after a short one that started later, so a strict
high-water mark would step over rows for ever. The watcher re-reads a small
overlap window each tick and discards ids it has already sent, which fails
toward a duplicate rather than a dropped transition. A duplicate changes
nothing on screen; a dropped one is a dashboard that is quietly wrong.

**Two things were only findable by loading the page**, and both had a green
suite behind them.

*A server component cannot hand a function to a client component.* The first
draft passed the pipeline's pill renderer and the headline's markup as render
props, which is ordinary React and fails in Next with "Functions cannot be
passed directly to Client Components" — a 500 on the whole route. `tsc` was
clean and the linter was happy. The components render their own markup now,
importing `StatusPill` directly.

*And the dashboard proxy compressed the stream.* This is the one worth
remembering, because the endpoint was provably correct the whole time. Next's
dev proxy re-encodes proxied responses and a compressor buffers, so the browser
connected, fired `open`, and then sat there receiving nothing — the page showed
"Updating live" beside counts that never moved. Measured through the dashboard
against the same endpoint, varying only the header every browser sends:

```text
curl -N                                          -> 124 bytes, the ready frame
curl -N -H 'Accept-Encoding: gzip, deflate, br'  ->  10 bytes, a gzip header
the same, direct to FastAPI (no proxy)           -> 124 bytes
```

`Cache-Control: no-transform` is the fix — the standards-compliant way to tell
an intermediary not to re-encode a body (RFC 9111 §5.2.2.6). It lives on the
response rather than in `next.config.ts` because it is a property of this body,
not of the dashboard: turning compression off globally would slow every other
page to rescue one route. `test_the_response_refuses_to_be_compressed` asserts
the header, since the behaviour belongs to a proxy that is not in the test
process — and a header nobody checks is exactly how this went missing.

The live proof, in a real browser with no reload: DOM `running 2, queued 2`, a
transition written by a *separate process*, DOM `running 3, queued 1`.

**A client disconnect can poison a pooled database connection**, and the
failure surfaces nowhere near the cause. Found by CI on this very PR, which is
the only reason it was found at all.

Starlette's `BaseHTTPMiddleware` — which `LocalhostOnlyMiddleware` is — cancels
the downstream task when the client goes away, and a browser goes away from a
stream whenever a tab closes or a page navigates. Unshielded, that cancel can
land *inside* the session's teardown: SQLAlchemy is part-way through returning
the connection to the pool, `do_terminate` is interrupted, and the next thing
to check that connection out gets `InterfaceError: connection is closed`.

What CI actually reported was a `TRUNCATE` failing in an unrelated fixture, two
tests after the one that caused it — a test whose only crime was closing the
response as soon as it had read the headers. Nothing in that traceback named
this route, the stream, or the client that left.

Two changes, and both are the same lesson:

- **The initial counts read is shielded** (`asyncio.shield(_read_counts())`),
  so the checkout, the query and the connection's return all complete even
  when the caller has gone. The frame is then delivered to nobody, which is
  correct — nobody is listening. Shielding only the *query* would leave the
  return exposed, which is the part that actually breaks.
- **The watcher is asked to stop, never cancelled.** `unsubscribe` used
  `task.cancel()`, which can arrive between sending a query and reading its
  result. It sets a flag and the wake event instead; the watcher exits at the
  top of its loop, having finished whatever poll was in flight. It costs
  nothing — the event is already set, so there is no sleep to wait out.

The local suite was green on both gate-0 runs before this and red in CI,
because the test that triggers it is the one added last. Worth remembering as
a shape rather than an incident: **a streaming endpoint runs on a task the
client can cancel, so anything it does to shared state has to be able to finish
without it.**

**`ASGITransport` cannot test this.** httpx builds the response only after the
ASGI app returns (`_transports/asgi.py` collects `http.response.body` into a
list), so the `client` fixture every other API test uses hangs at `__aenter__`
on an endpoint that never returns — before a single assertion runs.
`tests/test_status_stream.py` runs uvicorn on an ephemeral port inside the
test's own event loop instead: same process, so the patched sessionmaker still
applies, and real sockets, so incremental delivery and client disconnect are
the real thing. Disconnect is what stops the watcher, so mocking it would have
left the one property nobody would notice failing untested.

### The assistant's answer is a stream too

Added 2026-10-07. `POST /chat` sends nothing until the model has finished, so
the dock sat empty while the local model read the prompt and then wrote at
about 12 tokens a second. `POST /chat/stream`
(`apps/api/routers/chat_stream.py`) sends each piece as it is written, one
JSON object to a line, and closes with the reply `/chat` returns. The dock
reads it (`apps/web/src/lib/chat-stream.ts`).

Seen in a real browser through the dashboard's proxy, local model, the owner's
data. "What needs me?", model not yet loaded: first words at 11.3 s, finished
at 19.9 s, the text longer at 41 of 42 samples in between. A second question
with the model warm, asked by `curl` with a browser's `Accept-Encoding`: first
byte at 0.5 s, finished at 6.3 s, 68 pieces, not compressed. The model writes
no faster. What changed is when the first word shows.

Everything above about streams applied again, and four things are new:

- **The rules before the model are `/chat`'s own functions.** `prepare`,
  `provider_for` and `did_not_answer` in `routers/chat.py` are called by both
  routes: the §2.2 refusal, the crawl command, the provider checks and the
  no-fallback errors (§14). A refusal or a command is one closing frame and no
  model. `test_the_closing_frame_is_the_reply_the_plain_route_gives` compares
  the two replies whole.
- **Citations arrive last.** They are read out of the finished text (§14), and
  a label can be split across two pieces. The dock replaces what it showed
  while the answer was written with the finished reply.
- **The first piece is waited for before the response starts.** Until then
  there is a status code to answer with, so "Ollama is not running" is the
  same error, with the same message, that `/chat` gives. After that a failure
  is an `error` frame and there is no closing reply: half an answer is not
  listed with sources under it.
- **A reader who leaves stops the model.** Closing the stream closes the
  connection to Ollama, which stops writing. The route takes no request
  session: everything is read first, on a session opened and closed inside
  one shielded awaitable, for the reason the status stream shields its read.

  Two holes in that were found by review the same day, and both had passing
  tests around them. `stream_text` iterated the provider's stream without
  closing it, and closing a generator does not close the one it is reading
  from, so the connection stayed open until the garbage collector reached it;
  the test that existed closed the provider's stream directly. And the dock's
  fetch had nothing to abort it, so leaving the page left the browser reading
  and the model writing. The wrapper closes what it wraps now, and the dock
  withdraws its question when it unmounts. The second is held in source only:
  it was not watched in a browser.

Only the two Ollama providers stream. Gemini, Anthropic, OpenRouter and the
stub have no `stream` method, and `stream_text` sends their answer as one
piece, so a cloud answer still arrives all at once.

Not done: a model that is still being loaded sends nothing for as long as the
load takes, which is what the proxy's 330 s limit is for. And during the live
check the machine went to 17% free once, with the model, the API, the
dashboard and a headless browser all running, and the guard unloaded the
model; the second question's 30 s to first word includes that reload and is
not a measurement of the route.

## From §22: A question that names a kind of job

### `make api` could not survive its own reload

Found while building the above, and it looked like a hang in the code being
edited. `make api` runs uvicorn with `--reload`; a reload asks the old worker
to stop, uvicorn waits for open connections to close, and the dashboard holds
one that never does: the live status stream (§17). With any dashboard tab
open, the first saved file left the API accepting nothing. Its event loop was
idle in `kevent`, one connection sat on port 8000, and `/health` timed out.

`--timeout-graceful-shutdown 3` bounds the wait.
`tests/test_dev_api_reloads.py` holds it. A server started before the change
keeps the old behaviour until it is restarted.

## §26: what checking the assistant's wiring found

On 2026-10-06 every path the assistant has was run against the owner's
database: the fields the dashboard reads against the ones the API sends,
sixteen questions through `retrieve()`, thirteen through `/chat` on the local
model, each provider, and one through the dashboard's proxy. The fields agree,
the refusal, the crawl command, the four buttons and the application context
answered correctly, and the counts matched the database. Four things did not.

- **A citation in round brackets did not count.** Qwen3 writes "(P1)". An
  answer resting on four postings had all five filed under "not cited".
  `cited_labels` reads brackets holding labels and nothing else, so "(the P2
  band)" is still prose.
- **"skill" was searched for.** The owner typed "any companies required skill
  of kafka list them". The word is in 9.6% of descriptions, under the 30% that
  drops a common word, and in 0.00% of titles. A posting that says "skill" and
  nothing of Kafka took one of the five places. It is a framing word now, with
  "skills", "skilled", "needs", "needed", "needing" and "them" (§22).
- **"software engineer jobs in London" opened with three roles in San Jose.**
  Two causes. A place named in a question was only a keyword, matched the same
  in a location field as in a list of an employer's offices: a term in the
  location now counts twice, as one in the title does. And a posting whose
  whole title is in the question goes first, which for a question about
  software engineers is every posting titled "Software Engineer": when
  something more was asked and some of the role's postings say it, a posting
  goes first for its title only if it is one of them.
- **A remote role in India was inside the search area.** Its location field
  says "Remote" and its title says "(India)". A field that names no place is
  no evidence, and `area_exclusion` kept it: 50 open postings, one of them
  among five answers about Kafka. When the field is silent the title is read,
  and only for a place abroad. This is the one rule (§15), so the feed and the
  scoring gate read it too.

On the 112 benchmark questions (52 of the 56 postings still open) the right
posting was in the five shown for 39 natural questions against 38, and for 2
paraphrased against 2.

Seen and left alone:

- **The employer's own repeats take places among the five.** Roku lists
  "Software Engineer" in San Jose three times, as three requisitions. §25
  leaves them as rows; showing one of them is a decision for the owner.
- **"jobs at ZEIT" says there are none.** The row was set aside for Vercel's
  board (§25) and the assistant does not follow that to Vercel.
- **The picker offers providers that are not set up.** Anthropic has no key
  and `ollama_cloud` answered 402. Both fail with the provider's reason.
- **Qwen3 volunteered a list of "skills not on the résumé"** under a question
  about roles with AI in the title. The question is not a gap question and
  the context held no gap section: the model's own framing of a requirements
  excerpt.

Found by the pull request, not by this check: a secret scanner failed #125 on
one line of `tests/test_data_api_builder.py`, the expected output of the
connection-string builder, with a made-up username and password written out
as one string. The test assembles them now, and this paragraph does not quote
the line, since the scanner reads prose as well.

## §27: what the model is handed, and what the reply says for itself

What the model is handed, and what the reply says for itself:

- **The gap table's heading says what it was counted over**, with the two
  larger numbers it is not a count of. Prompt `assistant.system` is **v6**:
  this and §14's conversation memory were written as two v5s, and the merge
  is the version that carries both paragraphs.
- **Under each posting, how it asks for a skill the question named**
  (`matching/evidence.py`): required, preferred, named under no heading, or
  one of several options, with the posting's own line. Of the five shown for
  "Which jobs use Kafka?" one requires it.
- **`grounded` says context was supplied, not that the answer was checked.**
  `unsupported_citations` is the one check made, and the `postings_*` counts
  keep five numbers apart. A cited label that exists is not a supported
  claim.
- **A local model that fails says which way**: `ModelUnreachable`,
  `ModelMissing`, `ModelTimedOut`. None of them falls back.

### Two things the merge with `main` left between its halves

Found on 2026-10-12 by reading #140 after it landed. Each half was right
where it was written. The fault was at the join, and no test spanned it.

Added to the root file's §27 on `main` after these notes were moved; the
other of the two is in `.claude/rules/assistant-search.md`.

- **A posting read again from an earlier answer was supplied.** #136 gives
  the model the postings its earlier answers cited, labelled after the ones
  found now. The citation check knew only the ones found now, so "the one
  from before [P6]" was reported as a source the model made up, and the dock
  said so under the answer. `unsupported_citations` and `postings_supplied`
  count both. `tests/test_chat_grounding.py`.
