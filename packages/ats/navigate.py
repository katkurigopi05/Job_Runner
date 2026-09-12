"""Getting to the application form, and knowing when it is there.

Two things every adapter needs and none of them should spell out twice.

**Where the form lives.** Greenhouse serves the form on the posting page.
Lever, Ashby and Workable each put it on a separate route — `/apply`,
`/application`, `/apply`. The pipeline used to enumerate fields on whatever URL
the crawler recorded, which on three of the four ATSes is a page with no
application fields on it at all.

**When it has rendered.** All three of those routes are React applications.
`domcontentloaded` fires with an empty `<div id="root">`, so a check that runs
immediately finds nothing and the adapter reports "no application form found on
page" — a message that reads as a broken selector and sent a real run looking
at the wrong thing entirely.

Neither of these is ATS-specific knowledge leaking upward (§8): the *segment*
and the *selectors* stay in each adapter, and only the mechanics live here.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import structlog

from packages.ats.base import ManualCompletionRequired, SiteError

log = structlog.get_logger(__name__)

#: How long to give a React form to mount. Generous because the alternative is
#: a false "no form here" on a page that has one, and that failure is recorded
#: as `site_error` — a code that invites a retry of something never broken.
FORM_READY_TIMEOUT_MS = 15_000


def application_route(url: str, url_re: re.Pattern[str], segment: str | None) -> str:
    """The URL of the application form for a posting URL.

    Returns `url` unchanged when this adapter does not recognise it, when the
    form is on the posting page (`segment is None`), or when the URL is already
    the application route — a resumed run re-enters with the URL it ended on,
    and appending `/apply` twice is a 404.

    Query and fragment are dropped: boards hand out tracking parameters, and
    `?lever-source=hn/apply` is not a route.
    """
    if segment is None or not url_re.match(url):
        return url

    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    if path.endswith(f"/{segment}"):
        return url

    return urlunsplit((parts.scheme, parts.netloc, f"{path}/{segment}", "", ""))


async def wait_for_form(
    page: Any,
    *,
    form_selector: str,
    field_selector: str,
    captcha_selector: str,
    timeout_ms: int = FORM_READY_TIMEOUT_MS,
) -> None:
    """Block until the application form has actually rendered a field.

    Waiting for the *form* alone is not enough, and on Ashby it is worthless:
    its form selector is `body`, which is attached before any of the page
    exists. A field is the first evidence that the employer's questions are on
    the page and can be read.

    Raises:
        ManualCompletionRequired: a captcha is why nothing rendered. Checked
            before the timeout is reported so the owner is told to finish the
            form by hand rather than to retry a run that was never broken.
        SiteError: the form never appeared.
    """
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    try:
        form = page.locator(form_selector).first
        await form.wait_for(state="attached", timeout=timeout_ms)
        await form.locator(field_selector).first.wait_for(state="attached", timeout=timeout_ms)
    except PlaywrightTimeout:
        if await page.locator(captcha_selector).count():
            raise ManualCompletionRequired(
                "captcha present; finish this application by hand"
            ) from None
        # Never the page's HTML — this message is written to the task row (§10).
        raise SiteError("application form did not render before the timeout") from None


async def open_application(page: Any, adapter: Any, url: str) -> str:
    """Navigate to the application route if it is not the page we are on.

    Returns the URL now open. Same-page adapters (Greenhouse) cost no
    navigation, which matters: a second `goto` of the posting page would throw
    away a parse we already did.
    """
    target: str = adapter.application_url(url)
    if target != page.url:
        log.info("opening_application_route", ats=adapter.name, url=target)
        await page.goto(target, wait_until="domcontentloaded")

    await adapter.wait_for_form(page)
    return target


#: Enough candidates to find the application on any real careers page, few
#: enough that a pathological one cannot turn a lookup into a crawl.
MAX_FORM_CANDIDATES = 20


async def locate_form(page: Any, *, form_selector: str, field_selector: str) -> Any:
    """The element on the page that holds the employer's questions.

    Picks the candidate with the most fields on it rather than the first in
    document order. That is what a widened selector needs: Workable's live
    application form carries no `action` attribute — the fixture that passed
    for months did, because it was written beside the selector that reads it —
    so the selector has to admit a bare `form`, and a bare `form` also matches
    the site search box that precedes it. A search box has one field and an
    application has a dozen, so "the one with the questions on it" separates
    them without another site-specific rule.

    Raises:
        SiteError: nothing on the page holds a field.
    """
    forms = page.locator(form_selector)
    count = min(await forms.count(), MAX_FORM_CANDIDATES)

    best: Any = None
    best_fields = 0
    for index in range(count):
        candidate = forms.nth(index)
        fields = await candidate.locator(field_selector).count()
        if fields > best_fields:
            best, best_fields = candidate, fields

    if best is None:
        raise SiteError("no application form found on page")
    return best
