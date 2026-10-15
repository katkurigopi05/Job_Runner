# Notes on the dashboard

Moved here from the root `CLAUDE.md` on 2026-10-11, word for word, so they
load only when work touches this directory. "§N" below is a section of
the root file, which keeps every heading and says where each section went.

- The review screen's document panels are in `packages/tailor/CLAUDE.md`.

## §3: the frontend row, and the two effect libraries

The frontend line originally said shadcn/ui. It was built without it, and the
row now says so. The components are small and few, the theme is ported wholesale
from another project of the owner's, and a generator that writes files into the
tree buys little when there are a dozen of them. Recording the deviation matters
more than the choice: a stack table that describes something the repo does not
contain is worse than either option.

**Two effect libraries, since 2026-10-05.** `border-beam` and `thinking-orbs`
(MIT, no dependencies of their own, from `Jakubantalik/Libraries.dev`) are the
dashboard's first runtime dependencies beyond Next and React. The owner asked
for them and approved the two placements, and each has exactly one: the orb in
the assistant while a question is out, the beam round the heading of the card
at the head of `/review`. `globals.css` rule 2 is one meaning per accent, and a
travelling light is a louder accent than a colour; `tests/test_dashboard_effects.py`
holds the two placements and the exact pins. `/chat` grew 109 to 120 kB of
first-load JavaScript and `/review` 120 to 143 kB.

Four things about `border-beam` that only its built file and a screenshot
showed, each handled in `review-card.tsx`:

- Its `theme="auto"` reads the system preference and never `[data-theme]`, so
  with light picked in the toggle on a dark system it drew for dark.
  `lib/display.ts` resolves the theme by `globals.css`'s own rule and passes it.
- It reads a detected corner radius of zero as "not found" and falls back to
  16px, which rounded every card in the queue. The radius is passed.
- It sweeps by angle. Round the whole card, which runs to several screens, the
  light spent its cycle on the long sides and was out of view; it goes round
  the heading.
- Its travelling kind leaves reduced motion to the caller, so the beam is
  switched off there. Every card is wrapped and `active` chooses, because
  wrapping only the lit card would remount the next one as it moved up and drop
  the answers typed into it.

## §3: the dashboard binds loopback, and why

**This paragraph used to say `0.0.0.0`, and that is now reversed.** The reason
for the LAN bind was real — reading the review queue from a phone away from the
desk — and the reason for reversing it is that the cost was larger than this
paragraph understood. It described the risk as "who can reach the dashboard".
The actual reach was the **API**, in full, with no authentication.

`apps/web/next.config.ts` rewrites `/api/:path*` to `127.0.0.1:8000`, and
`apps/api/middleware.py` refuses non-loopback callers by reading the socket
peer. Through the rewrite that peer is always the Next server, on loopback. So
the guard never saw a network client and never had cause to refuse one. It is
not the `--proxy-headers` hole the middleware's docstring warns about; no header
is involved, and no misconfiguration. Binding the dashboard to the LAN was
sufficient on its own.

Measured, with `make api` on 127.0.0.1 and `make web` as it was:

```text
GET  http://127.0.0.1:8000/health        from a LAN address -> refused
GET  http://<lan-ip>:3001/api/health     from a LAN address -> 200
GET  http://<lan-ip>:3001/api/candidates from a LAN address -> 200   (PII)
GET  http://<lan-ip>:3001/applications   from a LAN address -> 200
POST http://<lan-ip>:3001/api/applications                  -> 400, not 401
```

The last line is the one that settles it. A validation error means the request
reached the handler: the write path — the one that submits real job
applications under the owner's name — was reachable from the network.

Two smaller corrections come with it. This paragraph claimed "`next start` is
unchanged and still binds localhost": it never did. `next start` took no `-H`,
and Next passes an undefined hostname straight to `server.listen`, which binds
every interface. And "set it back on an untrusted network" was advice nobody
can act on before the fact — the exposure is the default, and the café is
exactly where you forget.

The phone case is kept as an opt-in rather than deleted: `JOBRUNNER_WEB_HOST`
(and `JOBRUNNER_WEB_PORT`) override the default in both scripts, so
`JOBRUNNER_WEB_HOST=0.0.0.0 make web` is the old behaviour, typed out, on a
network the owner has decided to trust. `tests/test_dashboard_binds_loopback.py`
holds the default, and holds these documents to it — the stale `next start`
claim survived in two files precisely because nothing checked prose.

### A page on another site, through the owner's own browser

Found by the 2026-10-14 audit, confirmed live on 2026-10-15: the API, the
dashboard's `/api` proxy and its pages all answered `Host: evil.example` with
200. Binding loopback does not cover this. DNS rebinding points a domain the
attacker owns at 127.0.0.1, so a page on it reaches this machine from the
owner's own browser, which treats it as same-origin and lets it read the
replies. From the dashboard that is the review queue, and the approve action.

What the browser will not let the page change is the `Host` it sends.
Everything here is reached by `localhost` or an IP address, never by a domain
name, so a domain is refused. That includes a phone on the LAN under
`JOBRUNNER_WEB_HOST`, which uses the address. Two places, because neither
can see the other's requests:

- **`src/middleware.ts`** runs on every path, no `matcher`: 403 for a
  `Host` naming a domain, or a write whose `Origin` does (`null` included).
  The rule is `src/lib/local-host.ts`, which `tests/test_rebinding_guard.py`
  runs through `node` rather than restating.
- **`apps/api/middleware.py`** does the same for direct callers, and reads
  `X-Forwarded-Host` too. Next's proxy rewrites `Host` to the API's own
  address (`changeOrigin: true`) and puts the browser's there. Server
  actions call the API from loopback with no browser `Host` attached, which
  is why the dashboard needs its own check.

After: the rebound `Host` gets 403 from the dashboard and 401 from the API,
`localhost` and `127.0.0.1` still get 200, and the status stream still
arrives through the proxy uncompressed.

## From §15: What the gates do and do not prove

### Nested forms on the review card

Found while adding the editor, and worth recording because it was silent.

`TailoringCompare` renders forms of its own, and the review card wrapped it in
the approve `<form>`. An HTML parser drops a nested `<form>` start tag, so on
the server render those buttons were inert — the feature worked when reached by
client-side navigation and not on a fresh load, which is the hardest kind of
bug to believe a report of.

The approve form no longer wraps the document panels. Its button and note bind
to it by `form` id instead, and `Submitting` takes `pending` explicitly because
`useFormStatus` reads the enclosing form and a detached button has none. Served
HTML for `/review` now has form nesting depth 1.

### Using it felt tense, and two mechanical choices were most of the reason

The owner's words. Not that the dashboard was ugly — that applying for jobs
through it felt worse than it needed to. Worth measuring rather than
sympathising with: **56% of the interface's sentences contained a negation or
a warning.** Every reassurance was phrased as a prohibition, every empty state
opened with "Nothing", and the app talked mostly about what it would not do
and what the owner lacked.

Two of those were structural rather than verbal.

**The feed showed only what you lack.** `/matches` rendered `missing_terms`
three times and `matched_terms` zero times — the API had been sending both all
along. So the screen's answer to "how do I look for this job" was eight chips
of absent skills and nothing about the ones on the résumé. Fit now leads, in
the positive colour, on the face of the card; the gaps sit inside the
breakdown and no longer open with the word "not".

**The score graded the owner against an unreachable bar.** It is a cosine
similarity, and §15 already recorded that the shipped 0.75 threshold was
unreachable — the first real run peaked at 0.271. On the owner's current
database: **74 matches spanning 0.000–0.081, thresholds of 0.75 and 0.0.** A
big "8%" on every card reads as "you are a poor fit for everything", and it
does not mean that.

The first attempt at this replaced the number with a word and kept comparing
against the threshold, which is worse: with those numbers every posting is a
"long shot" under one profile and a "strong match" under the other. Both
collapse. **The label is rank within the feed** — measured spread on the live
data: 5 / 13 / 17 / 15 across four labels. Rank cannot degenerate, and "near
the top of what you are being shown" stays true whatever the absolute scale
turns out to be. Clearing the owner's own threshold is reported separately,
because that is the one number they actually set.

`legitimacy.py` reached this conclusion first, for the other score in this
app: "a tier and a findings table, never a number", because a number invites a
precision the thing does not have. This is the same argument arriving late.

The verbal half: the line on every page went from "Nothing submits without
your approval" to "You approve every application before it is sent" — the same
promise, and only one of them opens by naming what will not happen. The
warning that renders most often, work authorization "Unknown", went from three
lines to one. The home page's headline numbers were two counts of work not
done out of four; one is now work done. The ratio is 50% and should not be
driven lower by deleting real warnings — a résumé an ATS cannot parse needs to
say so.

`tests/test_feed_reads_as_encouraging.py` holds the two structural ones, since
both are a one-line revert away and neither would fail anything else.

### The tracker dropped five of seven outcomes, and looked fine doing it

Phase 6's whole deliverable. Found by screenshotting the dashboard rather than
reading it: the page rendered a heading, a paragraph, and nothing at all, while
the database held two rejected applications.

The column keys came from the wrong enum. `Classification` is what an inbound
*email* is; `Outcome` is what lands on the application, and the board was keyed
on the first:

| column key | what the API sends | matched |
|---|---|---|
| `rejection` | `rejected` | never |
| `info_request` | `info_requested` | never |
| — | `assessment` | no column |
| — | `acknowledged` | no column |
| — | `awaiting` | no column |

Five of seven, including the one §15 argues is the most time-critical: an
assessment is an opportunity with a deadline, and the window closes while the
tracker looks calm.

**It did not look broken, and that is the part worth keeping.** `columnFor`
returned a non-null key, so `tracked.length` was 2 and the "nothing submitted
yet" empty state did not fire either. The failure produced an empty `<div>`
between the header and the footer — no error, no zero, nothing to notice.

Two fixes rather than one. The keys are `Outcome` values now, and
`columnsFor` renders an unnamed column for any outcome nothing matched, so the
next value added to the enum surfaces unstyled instead of vanishing. A column
nobody designed is a much smaller problem than an application nobody can see.

`tests/test_tracker_columns.py` reads the TSX and the enum rather than a list
of expected strings, which would be a third place for the same drift, and it
is in `GATE6_TESTS`. Against the old keys it fails on five outcomes.

### The design pass, and what it turned up

`/matches` was a 31,000px wall and `/` was fine, which is what said the tokens
were not the problem. Four habits were, and they are written into
`globals.css` because a rule nobody can point at is a preference: **mono is
for values, not prose**; **one meaning per accent** (amber had drifted onto
eight missing-skill chips per card, so the "needs you" colour was the most
common thing on a card needing nothing); **proximity carries the grouping**;
**no boxes inside boxes**.

Two of the findings were not cosmetic at all, and both came from looking at a
rendered page rather than at source:

- The tracker, above.
- **The same score read `8%` on `/matches` and `0.081` on `/swipe`** — one
  number, two notations, on two screens the owner moves between while making
  the same judgement.

And one thing the screenshots caught that no test would have: `/matches`
returned **500** on a database where the `citizenship_status` migration had
not been applied. That is ordinary — a model column without its migration is
always a hard failure — but worth knowing that it takes the whole page rather
than degrading, because the review queue is what the owner opens first.

`profiles.citizenship_status` also had no control. The column, the schema, the
filter and the tests all shipped in one commit and the owner had no way to set
the value, so the citizenship filter could never fire on real data. It is on
`/profile` now, inside the work-authorization aside, and the screen states the
distinction the column exists for: this one filters, the box above it is what
gets copied onto a form.

### The dashboard put the API on the network, and the guard could not see it

Reported by a reader of the source and confirmed by running it. Worth recording
because the control that failed was working exactly as designed, and was
looking at the wrong end of the connection.

`apps/api/middleware.py` refuses non-loopback callers by reading the socket
peer, which cannot be forged. Its docstring names the one way that goes wrong —
`uvicorn --proxy-headers`, which makes the app trust `X-Forwarded-For`. That is
not what happened here. `next dev -H 0.0.0.0` bound the dashboard to every
interface, and `next.config.ts` rewrites `/api/:path*` to `127.0.0.1:8000`, so
the peer FastAPI saw was always the Next server, on loopback. No header, no
misconfiguration, no flag: the shipped default was enough.

The measurements are in §3. The line that settles it is
`POST /api/applications` answering **400, not 401** — a validation error means
the request reached the handler, so the endpoint that submits real applications
under the owner's name was reachable from the LAN.

Both scripts now bind `127.0.0.1`, and `JOBRUNNER_WEB_HOST` is the opt-in for
the phone case that motivated the original exposure. Two things are worth
keeping visible beyond the fix:

- **`next start` was never loopback-bound**, though §3 and `docs/USAGE.md` both
  said it was. It passed no `-H`, and Next hands an undefined hostname to
  `server.listen`, which binds everything. A wrong reassurance is worse than
  none: it is what a reader checks against before deciding whether they are
  exposed.
- **The guard is unchanged and still worth having.** With the dashboard on
  loopback it is what refuses a direct `uvicorn --host 0.0.0.0`. It was never
  wrong; it was answering a question about the proxy rather than about the
  person.

`tests/test_dashboard_binds_loopback.py` holds the default, and holds these
documents to it — the stale `next start` claim survived in two files precisely
because nothing checked prose. It allows a retraction to quote what it
retracts, because this file's convention is to record what a paragraph used to
say rather than edit it away.
