# Crawler database rules

These requirements apply to every board reader, aggregator, discovery job, and
description/location backfill in this package.

- Persist postings through `crawl._store`. It applies `location_fields.normalize`
  and the standing `Settings.search_us_only` admission rule. Discovery must use
  the same writer; new integrations must not insert `Posting` directly.
- Reject a **new** posting when `wholly_foreign` confirms every office is outside
  the US and US-only is enabled. Remote work in a foreign country is still foreign.
- Preserve any posting that offers a US office. Keep each office separately in
  `locations_json`; scalar country/state/work-mode fields describe values common
  to every office and stay NULL for mixed or unknown values.
- Keep unknown facts NULL. Never treat a missing location, `In-Office`, `Remote`,
  or `6 Locations` as proof of a US office. Unknown-location visibility follows
  the existing search policy; do not silently change that policy.
- A state code is a US postal code and requires `country_code='US'`. Country
  codes are canonical uppercase ISO alpha-2 values read from source evidence.
  Never infer location from a board's `en-US` locale or the employer's headquarters.
- Workday detail reads must preserve the primary and additional offices,
  explicit country/work-mode facts, and the original `listed_location` label.
  Cached detail facts must survive unchanged listing pages and bounded refreshes.
- Keep already-held records synchronized for liveness and preserve applications,
  resumes, decisions, and grading history. Do not turn a location change into a
  silent deletion or a false "job closed" event.
- Use the shared location rule in `locality.area_exclusion` for search results
  and counts. California permits on-site/remote; other US states require remote
  under the current setting. Storage admission and search visibility are separate
  stages, both controlled by the owner's settings.
- Respect the database location constraints. Fix invalid source-to-field mapping;
  never remove constraints or mark unknown values as verified to make a crawl pass.
- Before completing crawler changes, run `make check-crawler-locations` against
  the separate marked test database. New location formats need regression coverage
  for foreign rejection, US/mixed-office preservation, and unknown facts.

Schema changes also need a reversible Alembic migration and `alembic check`.
See [structured location documentation](../../docs/STRUCTURED_LOCATIONS.md).
