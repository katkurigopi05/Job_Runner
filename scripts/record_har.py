"""Re-record the Greenhouse HAR fixture.

    python -m scripts.record_har https://boards.greenhouse.io/<company>/jobs/<id>

The replay test in `tests/test_greenhouse_har.py` runs against recorded bytes
rather than markup we wrote. That is the point: a hand-written fixture drifts
from the site silently, which is how the react-select bug survived a green
suite for weeks. A recording can only go stale, and stale is visible — the
assertions start failing.

Re-record when Greenhouse changes its form and the replay test starts failing
for a reason that turns out to be real.

Recorded with `record_har_mode="minimal"`: enough to replay, without timings
and headers that make the fixture churn on every re-record.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

HAR = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "har"
    / "greenhouse-posting.har.zip"
)


async def main(url: str) -> int:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(record_har_path=str(HAR), record_har_mode="minimal")
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        forms = await page.locator("#application_form, form[id*='application']").count()
        print(f"url:   {url}")
        print(f"forms: {forms}")
        if forms == 0:
            print("FAIL: no application form on that page; nothing worth recording")
            await context.close()
            await browser.close()
            return 1

        await page.close()
        await context.close()
        await browser.close()

    print(f"wrote: {HAR}  ({HAR.stat().st_size // 1024}K)")
    print("Update RECORDED_URL in tests/test_greenhouse_har.py to match.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(sys.argv[1])))
