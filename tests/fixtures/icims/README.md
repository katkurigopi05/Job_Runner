# Public fixture provenance — 2026-11-18

Career page: https://jobs.aon.com/jobs

The site's referenced JavaScript: https://app.jibecdn.com/prod/search/4.11.226/main.js

List request: https://jobs.aon.com/api/jobs?page=1&limit=10

Read using `User-Agent: jobrunner` through `build_fetcher(shared=True,
delay_seconds=5, record_metrics=False)`. PoliteFetcher checked robots.txt
and reserved shared host slots, honoring any longer server delay. No login,
captcha, application, secured API, paid API or alternate identity was used.
Each list and the representative public detail returned HTTP 200.
No company/posting rows were written or promoted by these probes.

The fixtures retain source values; they omit unrelated keys, analytics IDs,
links and most list rows. Totals remain the real advertised totals, so these
trimmed lists are deliberately incomplete. Tests that need a complete small
board explicitly rewrite the total or offset and label those scenarios synthetic.

Aon advertised 1,073 public jobs. The first list retains two jobs. The
`aon-multiple.json` fixture retains job 107033 from page 2 and all seven offices;
`aon-page.html` retains the two vendor markers from the actual public page.
The site's search script calls `/api/jobs` with `page` and `limit` parameters.
Application login URLs and tracking metadata are omitted. The reader constructs
public `/jobs/{slug}` links and never fetches the `apply_url` field.
Legacy iCIMS portals are outside this observed Jibe API contract.

Original downloaded list SHA-256: `70c21d27b4f877ec98c94c3c8535190ce630971e74919a8bb46b60c673d593a5`.
