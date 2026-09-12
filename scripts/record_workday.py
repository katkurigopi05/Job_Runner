"""Record a real Workday board, so the fixture stops being a guess.

    python -m scripts.record_workday https://acme.wd5.myworkdayjobs.com/en-US/AcmeCareers

`tests/test_workday.py` runs against a payload written from the API's
documented shape, because the machine this was built on has no egress to
`*.myworkdayjobs.com`. CLAUDE.md §15 says what that is worth: the hand-written
Greenhouse fixture had a native `<select>` while the live board had moved to
react-select, so the adapter misread every dropdown and the suite stayed green
for weeks. The pagination logic is testable offline; the field names are not.

This fetches the first two pages through the ordinary polite fetcher — so
robots.txt and the §2.6 floor apply, which is why it is slow — and writes them
to `tests/fixtures/workday/`. Compare them against `_job()` in the test file
before trusting a Workday row in the registry.

It prints rather than asserts. What matters is whether `title`,
`externalPath`, `locationsText` and `bulletFields` are the keys a real tenant
sends, and that is a question for a person reading the output.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "workday"

#: Two, not one. The first page proves the field names; the second proves the
#: offset is actually honoured, which is the half a single page cannot show.
PAGES = 2


async def main() -> int:
    from packages.crawler.fetch import build_fetcher
    from packages.crawler.workday import PAGE_SIZE, api_url, parse_page, parse_site

    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    site = parse_site(sys.argv[1])
    if site is None:
        print(f"not a Workday careers URL: {sys.argv[1]}", file=sys.stderr)
        return 1
    host, tenant, board = site
    url = api_url(host, tenant, board)
    print(f"tenant={tenant} site={board}\n{url}\n(60s between pages — §2.6)\n")

    FIXTURES.mkdir(parents=True, exist_ok=True)
    async with build_fetcher() as fetcher:
        for page in range(PAGES):
            offset = page * PAGE_SIZE
            response = await fetcher.fetch(
                url,
                method="POST",
                json={
                    "appliedFacets": {},
                    "limit": PAGE_SIZE,
                    "offset": offset,
                    "searchText": "",
                },
            )
            if not response.ok:
                print(f"offset {offset}: HTTP {response.status}", file=sys.stderr)
                return 1

            path = FIXTURES / f"{tenant}-{board}-offset{offset}.json"
            path.write_text(response.text, encoding="utf-8")

            postings, total = parse_page(response.text, host, board)
            print(f"offset {offset}: {len(postings)} parsed of {total} total -> {path.name}")
            if not postings:
                # The keys we read are not the keys it sent. That is exactly
                # what this script exists to discover.
                try:
                    keys = sorted((json.loads(response.text).get("jobPostings") or [{}])[0])
                except (ValueError, IndexError, TypeError):
                    keys = []
                print(f"  parsed nothing — top-level posting keys were: {keys}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
