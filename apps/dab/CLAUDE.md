# Notes on Data API Builder

Moved here from the root `CLAUDE.md` on 2026-10-11, word for word, so they
load only when work touches this directory. "§N" below is a section of
the root file, which keeps every heading and says where each section went.

## §23: a second, generated API beside the written one

`make dab` runs Microsoft's Data API Builder (2.0.12, MIT, a `dotnet` tool)
over three sources and serves them on `127.0.0.1:5050`: REST with `$filter`,
`$select`, `$orderby`, `$first` and `$after`, a Swagger page at `/swagger`,
and GraphQL at `/graphql` with joins. The owner asked for it on 2026-10-06,
after the DP-800 course's section on the tool (their `DP-800DAB` workspace).

It answers questions nobody wrote a route for. The routes in `apps/api`
each do one thing; "open postings paying over 200,000, highest
first" or "my top matches with their company" was a `psql` session.

```text
GET /api/Posting?$filter=closed_at eq null and salary_min gt 200000&$orderby=salary_min desc&$first=3
{ matches(first: 3, orderBy: {score: DESC}) { items { score posting { title company { name } } } } }
```

It is a second door onto the database and does not inherit the first one's
guards, so five things are held by `tests/test_data_api_builder.py`:

- **Three sources: `companies`, `matches`, and `api_postings`.** Résumés,
  profiles, applications and recruiter mail are not served (§2.8). Adding a
  source means adding it to the test as well as the config.
- **Read only, twice.** The config grants `read` and nothing else, and the
  connection asks Postgres to start every transaction read-only. Measured
  with a scratch config that *allowed* writes: a delete came back
  `25006: cannot execute DELETE in a read-only transaction`.
- **This machine only.** `scripts/run_dab.py` sets the address and takes no
  setting for the host; `JOBRUNNER_DAB_PORT` moves the port. §3 records what
  a network bind nobody had read cost the dashboard.
- **No MCP endpoint and no telemetry.** The course's own rule is that an MCP
  endpoint is never left anonymous, and `apps/mcp` is this project's. The
  generated config also exported traces to wherever `OTEL_*` pointed.
- **Started by the launcher, never `dab start`.** The tool reads `.env` from
  its working directory. The project's holds every key there is, and the tool
  cannot parse it anyway: started in the repository root it crashed.

Four things only running it showed:

- **It cannot read a pgvector column.** `postings` stopped it at start-up
  ("Reading as 'System.Object' is not supported for fields having
  DataTypeName 'public.vector'"). `api_postings` is a view with the columns
  named and the embedding left out. A column added to `postings` later is not
  served until it is added to the view, and a migration that changes the type
  of a column the view uses has to drop the view first.
- **A view may not have relationships.** Declared `"type": "view"` it refused
  to start ("Cannot define relationship for entity: Posting") and
  `dab validate` crashed on a cast. It is declared `"type": "table"` with its
  key named, which is what the course's own config does for its view.
- **Port 5000, the tool's default, is macOS's AirPlay receiver.**
- **`salary_min` is in the posting's own currency and period.** The first
  query above returned rupee salaries in Hyderabad. Filter on
  `salary_currency` and `salary_period` too.

Not done: nothing in the dashboard or the assistant calls it, CI does not
have the tool installed and so never starts it, and the course's deployment
half (a container registry, Container Apps, Azure monitoring) is paid hosting
that §11 rules out.
