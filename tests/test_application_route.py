"""Getting to the form before reading it.

The pipeline opened the *posting* URL and enumerated fields straight after
`domcontentloaded`. On Greenhouse that happens to work: the posting page is the
form. On Lever, Ashby and Workable the form lives on a separate route, and on
all three it is rendered by React after the document is ready. So enumeration
ran against a page that had no application fields on it yet — and the adapters
reported "no application form found on page", which reads as a broken selector
rather than as a page we never navigated to.

A live probe of the corresponding application routes found 40 Lever controls
and nine Ashby fields once the render had landed. The fields were always there;
nothing had gone to look at them.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from packages.ats.ashby import AshbyAdapter
from packages.ats.base import SiteError
from packages.ats.greenhouse import GreenhouseAdapter
from packages.ats.lever import LeverAdapter
from packages.ats.workable import WorkableAdapter


@pytest_asyncio.fixture
async def page():
    from apps.worker.browser import ephemeral_page

    async with ephemeral_page() as value:
        yield value


# --- where the form lives -------------------------------------------------


@pytest.mark.parametrize(
    ("adapter", "posting", "expected"),
    [
        (
            LeverAdapter,
            "https://jobs.lever.co/qonto/8a0f1b2c-3d4e-5f60-7182-93a4b5c6d7e8",
            "https://jobs.lever.co/qonto/8a0f1b2c-3d4e-5f60-7182-93a4b5c6d7e8/apply",
        ),
        (
            AshbyAdapter,
            "https://jobs.ashbyhq.com/elevenlabs/8a0f1b2c-3d4e-5f60-7182-93a4b5c6d7e8",
            "https://jobs.ashbyhq.com/elevenlabs/8a0f1b2c-3d4e-5f60-7182-93a4b5c6d7e8/application",
        ),
        (
            WorkableAdapter,
            "https://apply.workable.com/open-252/j/6D0FEF463C",
            "https://apply.workable.com/open-252/j/6D0FEF463C/apply",
        ),
        # Greenhouse serves the form on the posting page itself. Appending
        # anything here would navigate away from the only page that has it.
        (
            GreenhouseAdapter,
            "https://boards.greenhouse.io/acme/jobs/4012345",
            "https://boards.greenhouse.io/acme/jobs/4012345",
        ),
    ],
)
def test_the_application_route_is_derived_from_the_posting(adapter, posting, expected) -> None:
    assert adapter.application_url(posting) == expected


@pytest.mark.parametrize(
    ("adapter", "already_there"),
    [
        (LeverAdapter, "https://jobs.lever.co/qonto/8a0f1b2c-3d4e-5f60-7182-93a4b5c6d7e8/apply"),
        (
            AshbyAdapter,
            "https://jobs.ashbyhq.com/elevenlabs/8a0f1b2c-3d4e-5f60-7182-93a4b5c6d7e8/application",
        ),
        (WorkableAdapter, "https://apply.workable.com/open-252/j/6D0FEF463C/apply"),
    ],
)
def test_an_application_url_is_left_alone(adapter, already_there) -> None:
    """A resumed run re-enters with the URL it ended on. Appending twice 404s."""
    assert adapter.application_url(already_there) == already_there


def test_a_query_string_does_not_become_part_of_the_path() -> None:
    """Boards hand out tracking parameters. `?src=hn/apply` is not a route."""
    assert (
        LeverAdapter.application_url(
            "https://jobs.lever.co/qonto/8a0f1b2c-3d4e-5f60-7182-93a4b5c6d7e8?lever-source=hn"
        )
        == "https://jobs.lever.co/qonto/8a0f1b2c-3d4e-5f60-7182-93a4b5c6d7e8/apply"
    )


def test_a_url_no_adapter_understands_is_returned_unchanged() -> None:
    """Never invent a route. An unparseable URL is handed back as it came."""
    assert LeverAdapter.application_url("https://example.com/jobs/1") == (
        "https://example.com/jobs/1"
    )


# --- waiting for the form to render ---------------------------------------


_LATE_FORM = """
<body><div id="root">Loading…</div>
<script>
  setTimeout(function () {
    document.getElementById('root').innerHTML =
      '<form id="application-form">' +
      '<li class="application-field"><label class="application-label">Full name</label>' +
      '<input name="name" type="text" required /></li></form>';
  }, 300);
</script></body>
"""


async def test_the_adapter_waits_for_a_form_that_renders_late(page) -> None:
    """This is the whole defect: the fields arrive after the document does."""
    await page.set_content(_LATE_FORM)

    # Without the wait there is nothing to enumerate.
    with pytest.raises(SiteError):
        await LeverAdapter().enumerate_fields(page)

    await LeverAdapter().wait_for_form(page)

    questions = await LeverAdapter().enumerate_fields(page)
    assert [q.key for q in questions] == ["name"]


async def test_waiting_on_a_page_with_no_form_reports_that(page) -> None:
    """A page that never renders a form is a site error, not a hang.

    It must not be `manual_completion_required`: nothing blocked us, and that
    status tells the owner to go and finish a form by hand.
    """
    await page.set_content("<body><p>This posting has been filled.</p></body>")

    with pytest.raises(SiteError):
        await LeverAdapter().wait_for_form(page, timeout_ms=500)


async def test_waiting_returns_at_once_when_the_form_is_already_there(page) -> None:
    """A form present at parse time must not cost a timeout."""
    await page.set_content(
        "<form id='application-form'><input name='email' type='email' /></form>"
    )
    await LeverAdapter().wait_for_form(page, timeout_ms=500)


# --- the worker actually goes there ---------------------------------------


_POSTING_PAGE = """
<div class="posting-header"><h2>Backend Engineer</h2></div>
<div class="location">San Francisco, CA</div>
<div class="posting-description">We are hiring.</div>
<a href="/acme/8a0f1b2c-3d4e-5f60-7182-93a4b5c6d7e8/apply">Apply for this job</a>
"""

_APPLY_PAGE = """
<form id="application-form">
  <li class="application-field"><label class="application-label">Full name</label>
    <input name="name" type="text" required /></li>
  <li class="application-field"><label class="application-label">Email</label>
    <input name="email" type="email" required /></li>
</form>
"""

_LEVER_POSTING = "https://jobs.lever.co/acme/8a0f1b2c-3d4e-5f60-7182-93a4b5c6d7e8"


async def test_the_pipeline_navigates_from_the_posting_to_the_form(page) -> None:
    """The posting page carries no fields. Enumerating it finds nothing.

    This is the defect end to end: a Lever posting URL is what the crawler
    records, and it is not the page the questions are on.
    """
    import asyncio

    from packages.ats.navigate import open_application

    def _serve(route):
        body = _APPLY_PAGE if route.request.url.endswith("/apply") else _POSTING_PAGE
        return asyncio.ensure_future(
            route.fulfill(status=200, content_type="text/html", body=body)
        )

    await page.route("**/*", _serve)
    await page.goto(_LEVER_POSTING, wait_until="domcontentloaded")

    adapter = LeverAdapter()

    # The posting page parses, and has nothing to fill in.
    posting = await adapter.parse_posting(page)
    assert posting.title == "Backend Engineer"
    with pytest.raises(SiteError):
        await adapter.enumerate_fields(page)

    opened = await open_application(page, adapter, _LEVER_POSTING)

    assert opened.endswith("/apply")
    assert [q.key for q in await adapter.enumerate_fields(page)] == ["name", "email"]


async def test_a_same_page_adapter_is_not_navigated_away_from(page) -> None:
    """Greenhouse's form is the posting page. A second goto would reload it."""
    import asyncio

    from packages.ats.navigate import open_application

    gotos: list[str] = []

    def _serve(route):
        gotos.append(route.request.url)
        return asyncio.ensure_future(
            route.fulfill(
                status=200,
                content_type="text/html",
                body="<form id='application_form'><input id='first_name' type='text'/></form>",
            )
        )

    await page.route("**/*", _serve)
    url = "https://boards.greenhouse.io/acme/jobs/4012345"
    await page.goto(url, wait_until="domcontentloaded")
    before = len(gotos)

    assert await open_application(page, GreenhouseAdapter(), url) == url
    assert len(gotos) == before


# --- which element on the page is the application form --------------------


_WORKABLE_LIVE_SHAPE = """
<body>
  <form role="search"><input name="q" type="text" /></form>
  <main>
    <form class="styles--2I-rr">
      <label>First name<input name="firstname" type="text" required /></label>
      <label>Email<input name="email" type="email" required /></label>
      <label>Why do you want to work here?<textarea name="QA_10337894"></textarea></label>
    </form>
  </main>
</body>
"""


async def test_the_workable_form_is_found_without_an_action_attribute(page) -> None:
    """The live form carries no `action`; the fixture that passed did.

    `form[action*='/apply']` matched the hand-written fixture and nothing on
    the real page, so a route with eight controls on it was reported as having
    no application form. The §15 pattern again: the fixture was written beside
    the selector that reads it.
    """
    await page.set_content(_WORKABLE_LIVE_SHAPE)

    questions = await WorkableAdapter().enumerate_fields(page)

    assert [q.key for q in questions] == ["firstname", "email", "QA_10337894"]


async def test_a_search_box_is_not_mistaken_for_the_application(page) -> None:
    """Widening the selector must not hand back the first form on the page.

    A site search box appears before the application in document order, so
    "first match wins" would enumerate one field named `q` and call the
    employer's questions answered.
    """
    await page.set_content(_WORKABLE_LIVE_SHAPE)

    keys = {q.key for q in await WorkableAdapter().enumerate_fields(page)}

    assert "q" not in keys
