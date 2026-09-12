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

from packages.ats.base import (
    ManualCompletionRequired,
    PostingGone,
    SiteError,
    UnsupportedSiteError,
)

log = structlog.get_logger(__name__)

#: How long to give a posting page to render before parsing it. Shorter
#: than the form wait because nothing here fails when it elapses: a
#: withdrawn posting legitimately has no title, and every posting would
#: otherwise pay this in full on its way to `job_closed`.
POSTING_READY_TIMEOUT_MS = 8_000

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

    # Compared against the URL we were *given*, not against `page.url`. The
    # caller has already navigated to `url`, and a board that redirects leaves
    # `page.url` pointing somewhere else — so comparing with the landing
    # address made every same-page adapter navigate a second time, throwing
    # away the parse it had just done and spending another request on it.
    if target != url and target != page.url:
        log.info("opening_application_route", ats=adapter.name, url=target)
        await page.goto(target, wait_until="domcontentloaded")

    _check_still_ours(adapter, page.url, adapter.external_id(url))

    await adapter.wait_for_form(page)
    return target


def _check_still_ours(adapter: Any, landed_on: str, external_id: str | None) -> None:
    """Refuse a page the employer redirected us off the ATS onto.

    Without this the run waits for a form that is not there and fails as
    `site_error` — our side is broken, retry this — when neither half is true.

    Which of the two honest verdicts it is turns on whether the page we landed
    on still names this job:

    - **It does.** The employer hosts the application on their own site.
      `job-boards.greenhouse.io/stripe/jobs/8172487` answers 200 at
      `stripe.com/careers/listing/abuse-investigator/8172487?gh_jid=8172487`,
      with no form on it at all — and Stripe does this for all 635 of its
      postings, live ones included. `import_portals.py` already refuses a
      bespoke careers page for the same reason, so `unsupported_site` it is.
    - **It does not.** The posting is gone.
      `job-boards.greenhouse.io/cloudflare/jobs/7168950` lands on
      `cloudflare.com/careers/#open-roles`, the careers index, naming no job.
      Cloudflare's board is fine; this role was taken down. Calling that
      `unsupported_site` would libel a board we poll successfully.

    A redirect *within* the ATS is not an exit at all: `boards.` to
    `job-boards.` is Greenhouse's own move and both URLs are still ours.
    """
    if adapter.matches(landed_on):
        return

    if external_id and external_id in landed_on:
        log.info("employer_hosts_its_own_application", ats=adapter.name, landed_on=landed_on)
        raise UnsupportedSiteError(
            f"{adapter.name} redirected to a page it does not serve; "
            "this employer's application lives on their own site"
        )

    log.info("posting_redirected_away", ats=adapter.name, landed_on=landed_on)
    raise PostingGone(
        f"{adapter.name} redirected away from this posting to a page that does not "
        "name it — the role has been taken down"
    )


async def wait_for_posting(page: Any, *, title_selector: str, timeout_ms: int) -> None:
    """Give the posting a chance to render. Never raises.

    Best-effort on purpose. A withdrawn posting has no title, and turning that
    into an exception would report `site_error` — our side is broken, retry
    this — for the one outcome that is both expected and permanent. So this
    waits, and then `parse_posting` reports whatever is actually there.

    Worth having even so: without it, a live Ashby posting parsed to a None
    title and an empty `description_raw`, and the application was then tailored
    against nothing and filtered on a location that had not been read.
    """
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    try:
        await page.locator(title_selector).first.wait_for(state="attached", timeout=timeout_ms)
    except PlaywrightTimeout:
        log.info("posting_did_not_render", timeout_ms=timeout_ms)


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


#: A description is long. Reading more than this many candidates to find the
#: longest one is a sign the selector is matching the whole page.
MAX_BODY_CANDIDATES = 40


async def longest_text(page: Any, selector: str) -> str | None:
    """The richest of the elements a selector matches, not the first of them.

    Lever splits a posting across eight `.section-wrapper .section` blocks and
    the first is the header, so `.first` reported a 109-character job
    description for a 4,967-character posting. That is the quiet kind of wrong:
    it is not empty, so nothing downstream looks broken — the tailorer simply
    has almost nothing to work from and the ATS keyword score almost no
    vocabulary, and both report that as a weak match.

    Same rule as `locate_form`, for the same reason: when a selector admits
    several candidates, the useful one is the one with the content on it.
    """
    nodes = page.locator(selector)
    count = min(await nodes.count(), MAX_BODY_CANDIDATES)

    best: str | None = None
    for index in range(count):
        text = (await nodes.nth(index).inner_text()).strip()
        if best is None or len(text) > len(best):
            best = text

    return best or None
