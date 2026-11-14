# Structured posting locations

`postings.location` holds the employer's location text. Workday detail reads
replace count labels such as `2 Locations` with the actual offices and preserve
the original label in `listed_location` for cache validation.

- `country_code`: ISO country code common to all offices; null for mixed or unknown countries.
- `state_code`: US postal state code common to all offices; null for mixed or unknown states.
- `is_remote`: true for declared remote work, false for declared on-site/hybrid work,
  null when unknown or different between offices. Absence of “remote” does not mean false.
- `locations_json`: each source office with its raw label, country, state and work mode.
- `location_version`: version of the local extractor that populated the fields.

Search, matching, chat candidates and complete counts use the same location rule.
Each office is considered separately, so a California office can keep a posting
that also offers foreign offices. Missing fields retain the existing unknown-location
policy. A foreign posting remains foreign even when remote.

Populate stored rows locally, without fetching pages or changing source text:

```sh
python -m scripts.extract_locations
```

Refresh unresolved Workday locations, including jobs whose descriptions are already stored:

```sh
python -m scripts.backfill_descriptions --locations --ats workday --dry-run
python -m scripts.backfill_descriptions --locations --ats workday
```

The refresh uses the existing robots, rate-limit, retry and indexing behavior.
`--limit 1 --no-index` reads at most one pending job per employer without starting indexing.
New crawls also resolve offices within their ordinary detail budget.

## Enforcement

All crawler and discovery writes go through `crawl._store`. Database migration
`a3c5e7f9b1d2` rejects malformed country/state codes, states without a US country,
invalid office JSON, nonpositive extractor versions, and scalar summaries that
contradict their offices. Versioned rows require an office array; legacy unknown
rows remain valid. US-only admission follows the runtime setting so foreign
history and explicitly widened searches remain supported.

Run `make check-crawler-locations` after crawler changes. The same tests run in
gate 5 and the complete CI suite. Permanent editing requirements live in
`packages/crawler/AGENTS.md`.

Migration `f2b4d6e8a0c1` only adds nullable columns and an index. Its downgrade
removes those derived columns; the original source text survives. The missing
historical revision `b5d7f9a1c3e6` was recovered from the live schema's existing
`posting_chunks.overlap SMALLINT NOT NULL DEFAULT 25` column, without resetting
the database's revision marker.
