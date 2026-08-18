"""Gate 1's offline half, replayed from recorded traffic — CLAUDE.md §9.

Phase 1 asks for "a recorded-HAR replay test [that] runs the same flow offline
in CI". The rest of the Greenhouse suite uses `set_content` fixtures, and those
have already proved too polite once: the hand-written fixture had native
`<select>` elements while the live board had moved to react-select, so the
adapter classified every dropdown as free text and the suite stayed green.

This replays the bytes Greenhouse actually served — same markup, same scripts,
no network. A fixture cannot drift from the site; a recording can only be stale,
which is a visible condition rather than a silent one.

Re-record with:

    python -m scripts.record_har https://boards.greenhouse.io/<company>/jobs/<id>
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from packages.ats.base import ManualCompletionRequired, QuestionKind
from packages.ats.greenhouse import GreenhouseAdapter

HAR = Path(__file__).parent / "fixtures" / "har" / "greenhouse-posting.har.zip"

#: The posting the HAR was recorded from.
RECORDED_URL = "https://boards.greenhouse.io/figma/jobs/5364702004"

pytestmark = pytest.mark.skipif(not HAR.is_file(), reason="HAR fixture not recorded")


@pytest_asyncio.fixture
async def replayed_page():
    """A page served entirely from the recording. No network."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context()
        # not_found="abort" so a request the recording does not cover fails
        # loudly instead of silently reaching the internet mid-test.
        await context.route_from_har(HAR, url="**/*", not_found="abort")
        page = await context.new_page()
        try:
            yield page
        finally:
            await context.close()
            await browser.close()


async def test_the_real_form_is_found(replayed_page) -> None:
    await replayed_page.goto(RECORDED_URL, wait_until="domcontentloaded")

    assert await replayed_page.locator("#application_form, form[id*='application']").count() == 1


async def test_enumerate_fields_against_recorded_markup(replayed_page) -> None:
    """The field list Greenhouse really serves, not one we wrote."""
    await replayed_page.goto(RECORDED_URL, wait_until="domcontentloaded")

    try:
        questions = await GreenhouseAdapter().enumerate_fields(replayed_page)
    except ManualCompletionRequired:
        pytest.skip("the recording contains a captcha; §2.5 stops before enumeration")

    keys = {q.key for q in questions}
    # Core fields every Greenhouse form has.
    assert {"first_name", "last_name", "email"} <= keys

    # The regression this file exists for: dropdowns are react-select, and a
    # fixture written by hand had native <select> instead.
    assert any(q.kind is QuestionKind.SINGLE_SELECT for q in questions), (
        "no dropdown detected in recorded markup — react-select classification "
        "has regressed, which is exactly what a hand-written fixture missed"
    )


async def test_widget_internals_stay_out(replayed_page) -> None:
    """intl-tel-input's country search is not a question the employer asked."""
    await replayed_page.goto(RECORDED_URL, wait_until="domcontentloaded")

    try:
        questions = await GreenhouseAdapter().enumerate_fields(replayed_page)
    except ManualCompletionRequired:
        pytest.skip("the recording contains a captcha; §2.5 stops before enumeration")

    assert not any(q.key.startswith("iti-") for q in questions)
