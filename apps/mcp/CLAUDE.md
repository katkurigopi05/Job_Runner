# Notes on the MCP server

Moved here from the root `CLAUDE.md` on 2026-10-11, word for word, so they
load only when work touches this directory. "§N" below is a section of
the root file, which keeps every heading and says where each section went.

- A grade's offer (§27) is in `packages/core/CLAUDE.md`.
- The feed over MCP, `my_matches` (§18), is in `packages/matching/CLAUDE.md`.

## §9, Phase 4: the MCP tools

**The two tools that decide a parked application make the client ask**
(2026-10-07). Over MCP the one calling `approve_application` or `submit_otp`
is a model, and "only call it when the owner has actually decided" was a
sentence in a docstring. Two things hold it now, and `tests/test_mcp.py` keeps
them naming the same tools:

- **The server marks them.** Their `tools/list` entries carry
  `_meta["anthropic/requiresUserInteraction"]: true` (`ASKS_THE_OWNER` in
  `apps/mcp/server.py`). Claude Code 2.1.214 and later prompts on every call
  to such a tool in every permission mode, past any allow rule, with no
  "don't ask again"; the mode that never prompts refuses the call.
- **`.claude/settings.json` asks too**, for a build that does not read the
  mark. An ask rule prompts in every mode and wins over an allow rule. It
  names a tool as `mcp__jobrunner__<tool>`, so renaming the server in
  `.mcp.json` would leave rules that match nothing; the test reads both files.

What this does not cover: another MCP client reads neither, and
`reject_application` is not marked. It is terminal but sends nothing, and a
prompt on every tool the review gate has would be read past. This machine's
`.claude/settings.local.json` enables the `jobrunner` server. The prompt itself
has not been watched appearing in Claude Code; the mark is asserted on the
wire entry, which is what the client reads.

This came out of looking at Sentience Governor, which records what an agent
did against what it declared and reports afterwards. A record of an approval
is no use once the form has gone.

**What the owner must supply, the owner types** (2026-10-07, the same day).
`approve_application` took the missing answers as an argument and `submit_otp`
took the code. The caller is a model, so a model typed what went on the
employer's form. A question the pipeline could not map can be a
work-authorization one, which §2.2 says is never model-written; the server's
instructions asked for that, and an instruction is a request.

Both are now filled by a resolver that asks the person
(`_owners_answers`, `_owners_code`): the client shows a form, each field
titled with the employer's exact wording, a question with choices offering
those choices and no others. Neither value is in its tool's input schema, so
there is no argument for a model to fill. Four things to keep:

- **A closed form approves nothing.** Decline and cancel post nothing and say
  so; nothing is guessed in the form's place (§2.4).
- **An empty field is not an answer.** It is left out, the question stays
  open, and the pipeline parks again.
- **A client that cannot show a form is refused**, with `/review` to go to.
  The questions are not handed to the model as a fallback.
- **Written through the library's resolver, not `ctx.elicit`.** Calling
  `ctx.elicit` inside the tool works on protocol 2025-11-25 and raises
  `NoBackChannelError` on 2026-07-28, in process and over stdio; the resolver
  form is carried by both. `tests/test_mcp.py` runs every case under both.

Not seen: Claude Code's own dialog. The server is enabled on this machine, but
what was run is the library's client answering the form in both protocols.

**The tracker over MCP reads and does not write** (2026-10-07). "What needs a
follow-up this week?" had no tool. The tracking API has eleven routes and the
cadence report is a twelfth, and an assistant reached none of them.
`follow_ups` lists open tasks and the submitted applications nobody has
answered; `application_tracking` is one application's tasks and contacts.
`tests/test_mcp_tracking.py` holds four things:

- **A task with no date is listed.** A recruiter's reply that says
  "interview" creates a task with no date on purpose
  (`packages/tracking/tasks.py`), and a list of what is due inside a window
  leaves it out. It is returned under `undated`.
- **The tool has no clock.** It asks `/tasks` once with the window and once
  without. What the windowed list holds is due or overdue by the route's own
  rule, and the rest is told apart by whether it has a date at all.
- **A contact is sent as name, relationship, company and role.** Their email,
  phone and profile link, and the owner's notes on them, are not. A contact is
  somebody else, and over MCP the reader is a model that is not on this
  machine; §14 says the same of their mail, and the calendar export already
  carries no contact's details. The test asserts the exact keys.
- **Only GET.** Adding a task, ticking one off and linking a contact stay on
  the dashboard. Every parameter sent is held to its route's signature, as
  `my_matches` holds its filters (§18).

Seen on the way and not fixed: `/tracker` asks for tasks due within 14 days,
so the same undated task is missing from its upcoming list. It shows on the
application's own page.

**Fixed on 2026-10-08.** `GET /tasks` takes `include_undated`, the tracker
asks with it, and a task with no date is listed first as "no date yet": one
nobody has dated may be sooner than everything under it, and no calendar will
ever remind anyone of it. The markup had always handled a missing date. The
request never let one through. Without the flag the route answers as before,
which `follow_ups` relies on. Seen in a browser against the test database: the
interview task a recruiter's reply creates, then the overdue one, then the
one due in three days.

Not done: a task names its application by URL. No tracking route carries the
company or the role.

Seen on the owner's data the same day, over stdio with the API up: nothing
waiting, and no task or contact on the one application tried. The grouping
and the contact cut have been seen on test rows only.

**An id handed to a tool could name another route** (2026-10-07, found beside
the tracker tools). Every tool that takes an id wrote it bare into the path it
asked the API for, and httpx resolves `..` before it sends. The one calling is
a model, and a posting or a reply it has read can tell it what to pass.
Measured against the real app, before the fix:

```text
application_history(application_id="../candidates?")
    asked for GET /candidates, returned the candidate list under "events"
edit_application_resume(application_id="../resumes/<id>/edit?", sections=...)
    asked for POST /resumes/<id>/edit, stored an invented line as version 2
```

The second is §2.1. `edit_application_resume` runs the fabrication guard
because a model is typing; the résumés page's route does not, because there
the owner is; and the two take the same body (§15, *Editing the résumé on the
review screen*). `test_the_tool_cannot_turn_the_guard_off` held the tool's
arguments and was green throughout. The same shape read `/inbox` and
`/contacts`: the recruiter mail the server has no tool for, and the contact
details the tracker tool above had just been written to leave out.

Every id now goes through `_id` on its way into a path
(`apps/mcp/server.py`). `tests/test_mcp_ids.py` holds three things:

- **An id is letters, digits, hyphens and underscores.** One part of a path,
  so it cannot climb, add a part, or turn the rest into a query. Whether it
  is anybody's id is still the API's to say, with its own error.
- **Nothing is formatted into a path without it.** Read from the source. The
  tracker tool above was written without it, and no list of tools holds one
  that does not exist yet.
- **A refused id is not repeated back**, and for `submit_otp` it is refused
  before the owner is asked to type a code.

The approval gate was not reachable this way: `/review` needs `approve: true`
in the body, a tool's body is its own, and the only tool that sends it is the
one that asks the owner. Other routes that write were reachable, and they were
not each tried. Not done: `ResumeEdit` still accepts a `guard` field it does
not read, and nothing in the API tells an assistant's request from the
dashboard's.

**Status questions over MCP, and a third tool that asks** (2026-10-08).
"What left my machine this week?", "is my setup healthy?", "how did the week
go?" and "is the crawler working?" had no tool. `audit_trail`, `setup_health`,
`weekly_digest` and `crawl_status` read; `start_crawl` queues one crawl.
`tests/test_mcp_status.py` holds them. Five things to keep:

- **The audit summary takes a number of days**, which it did not: it was the
  whole trail or nothing, so "this week" could not be asked. An entry whose
  time cannot be read is counted, because leaving an upload out of the answer
  is the worse mistake.
- **It counts uploads by task as well as by provider.** §2.8 permits one
  upload, the tailoring call, and a count by provider cannot show one made
  for something else.
- **Counts and sizes, no digests.** The trail keeps digests so the owner,
  holding the original, can prove what was sent. `/audit/verify` has no tool:
  checking a text would mean handing it to the assistant.
- **`POST /crawl` exists, and `routers/crawl.py` records its second
  reversal.** That file said starting a crawl stays at a terminal, then that
  the router stays read-only. The MCP tools go through HTTP on purpose, so a
  tool that starts a crawl needs a route to ask. It calls `request_crawl`,
  the one door, and a test reads the source to keep it from enqueueing for
  itself.
- **`start_crawl` makes the client ask**, as `approve_application` and
  `submit_otp` do, in `ASKS_THE_OWNER` and in `.claude/settings.json`. §14
  keeps "run crawler" a typed command matched in code and never inferred from
  a question. Over MCP the one deciding is a model, and an empty feed is
  exactly what would make one decide to. The tool takes no argument and
  posts no body.

Left out: the setup page's registry repair, which rewrites company rows and
is previewed there first. Not seen: Claude Code's prompt for `start_crawl`.
The mark is asserted on the tool's `tools/list` entry.

**Grading postings in a conversation, with the model kept out of the grade**
(2026-10-08). §15's labeling loop needs a hundred or more of the owner's
grades and has none. `next_to_grade`, `grade_posting` and `grading_progress`
are the same loop as `/label`, reached by talking. What is different here is
who is calling: a grade a model chose would be the ranker marked by another
model and exported as `Provenance.OWNER`, the provenance a benchmark trusts
most. `tests/test_mcp_grading.py` holds four things:

- **The owner picks the grade in a form.** It is not an argument of
  `grade_posting`. The four choices are built from
  `labels.RELEVANCE_SCALE`, the scale the metrics read. A closed form grades
  nothing, and a client that cannot show a form is sent to `/label`.
- **The model is not shown the score or the stream.** This differs from
  `/label`, which shows the owner both on the card as its audit trail. There
  they are two values beside the posting. In a conversation the model writes
  everything the owner reads about the posting, and one that knows the
  ranker's opinion can lean its description that way without quoting it. It
  is one tuple to widen (`_GRADING_FIELDS`).
- **The stream still goes back with the grade.** `routers/labels.py` records
  `unseen` only when a serve attests it, and its comment named the MCP tools
  among the callers that could never attest. The server remembers what
  `/labels/next` told it about each posting it offered and sends that back
  itself.
- **A grade lands only on a posting this server offered.** The form says
  which posting it is about, from the server's memory of the offer and not
  from the conversation. An id it never offered has no name to show, so it
  is refused before the owner is asked anything.

What a tool cannot stop: the model saying in the conversation which grade it
would give. The tool descriptions and the server's instructions ask it not
to, and that is a request. Also not done: the memory of what was offered is
the process's, so after a restart a posting has to be offered again before
it can be graded. Not seen: Claude Code's own form. The library's client
answered it in both protocols.

**An offer is for one profile, and was remembered by posting alone**
(2026-10-08, found by review). Two profiles can be served the same posting,
each from a stream of its own. The server kept what it had offered by posting
id, so the second offer replaced the first, and a request that arrived while
a form was open moved the grade:

```text
next_to_grade(profile_id=p-1)    offers post-1, stream unseen
grade_posting(post-1)            the form opens
next_to_grade(profile_id=p-2)    offers post-1, stream confident
the form is answered             POST /labels: profile p-2, stream confident
```

Run on both protocols before the fix, with that result on each. What is
remembered is keyed by profile and posting now, and `grade_posting` takes the
`profile_id` that `next_to_grade` was given. Three things to keep:

- **The profile is an argument and the grade still is not.** Which profile is
  being graded for was already the caller's to say, in `next_to_grade`.
- **Holding the offer for the length of one call would not have been
  enough.** On protocol 2026-07-28 the library runs the resolver again once
  the form is answered, so what was read before the form is read a second
  time after it. The key is what makes both reads the same offer.
- **A posting named for a profile it was not offered to is one nobody
  offered**, and is refused before the owner is asked. Naming no profile
  after offering under one is refused the same way: the tool does not pick.

Not done: the form does not say which profile it is for. The server knows a
profile by its id and not by its label.

`apps/mcp/server.py` is past 1,200 lines with these. Splitting it means
moving the shared `server` and `_client` into a module of their own, which
every MCP test's monkeypatch touches, so it is a change of its own.
