"""Parsing the posting before it has rendered.

The same defect as the application form, one step earlier and with worse
consequences. `parse_posting` ran straight off `domcontentloaded`, and on a
live Ashby posting that is an empty `<div id="root">`:

    immediately after domcontentloaded:  h1 count: 0
    after 3s:                            h1 count: 1  "Accounts Payable Specialist"

So title, location and `description_raw` all came back None. Nothing failed.
The application went on to be tailored against an empty job description, scored
for ATS keywords against no vocabulary, and filtered on a location that was not
read — and every one of those reads as "this posting is a poor match" rather
than as "we never looked at it".

The selectors turned out to be right all along: once rendered,
`[class*='_description']` holds 4,613 characters of the real posting.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from packages.ats.ashby import AshbyAdapter
from packages.ats.greenhouse import GreenhouseAdapter
from packages.ats.lever import LeverAdapter
from packages.ats.workable import WorkableAdapter


@pytest_asyncio.fixture
async def page():
    from apps.worker.browser import ephemeral_page

    async with ephemeral_page() as value:
        yield value


_LATE_POSTING = """
<body><div id="root"></div>
<script>
  setTimeout(function () {
    document.getElementById('root').innerHTML =
      '<h1>Accounts Payable Specialist</h1>' +
      '<div class="_description">We are hiring an accounts payable specialist.</div>';
  }, 300);
</script></body>
"""


async def test_a_late_rendered_title_is_waited_for(page) -> None:
    await page.set_content(_LATE_POSTING)

    # Without the wait there is nothing on the page to read.
    assert (await AshbyAdapter().parse_posting(page)).title is None

    await AshbyAdapter().wait_for_posting(page)
    posting = await AshbyAdapter().parse_posting(page)

    assert posting.title == "Accounts Payable Specialist"
    assert "accounts payable specialist" in (posting.description_raw or "").lower()


@pytest.mark.parametrize(
    "adapter_cls", [GreenhouseAdapter, LeverAdapter, AshbyAdapter, WorkableAdapter]
)
async def test_waiting_on_a_posting_that_never_renders_does_not_raise(page, adapter_cls) -> None:
    """A withdrawn posting has no title, and that is an answer, not a fault.

    Raising here would turn `job_closed` — expected and permanent — into a
    timeout that reads as `site_error` and invites a retry. So this is
    best-effort: it waits, and then lets the parse report whatever is there.
    """
    await page.set_content("<body><p>This posting has been filled.</p></body>")

    await adapter_cls().wait_for_posting(page, timeout_ms=300)

    assert (await adapter_cls().parse_posting(page)).title is None


async def test_a_posting_already_rendered_costs_no_wait(page) -> None:
    await page.set_content("<h1>Backend Engineer</h1>")

    await AshbyAdapter().wait_for_posting(page, timeout_ms=300)

    assert (await AshbyAdapter().parse_posting(page)).title == "Backend Engineer"


# --- Ashby's location is a section, not the whole sidebar -----------------


#: Trimmed from the live ElevenLabs posting. The left pane is a list of
#: `<h2>heading</h2><p>value</p>` sections, all inside one `[data-highlight]`.
_ASHBY_DETAILS = """
<h1>Accounts Payable Specialist</h1>
<div class="_details" data-highlight="none">
  <div class="_section"><h2 class="_heading">Location</h2>
    <p>United Kingdom; Germany; New York; Poland</p></div>
  <div class="_section"><h2 class="_heading">Employment Type</h2><p>Full time</p></div>
  <div class="_section"><h2 class="_heading">Location Type</h2><p>Remote</p></div>
  <div class="_section"><h2 class="_heading">Department</h2><p>Operations</p></div>
</div>
<div class="_description">We are hiring.</div>
"""


async def test_the_location_is_the_location_not_the_whole_sidebar(page) -> None:
    """Reading the pane whole gave a location of

        'Location United Kingdom; Germany; New York; Poland Employment Type
         Full time Location Type Remote Department Operations'

    which `locality.py` then has to make a region decision from. Hard filters
    *exclude*, so a garbled location is silent: the posting is dropped and
    nothing says why.
    """
    await page.set_content(_ASHBY_DETAILS)

    posting = await AshbyAdapter().parse_posting(page)

    assert posting.location is not None
    assert posting.location.startswith("United Kingdom; Germany; New York; Poland")
    # The rest of the pane is somebody else's field.
    for noise in ("Employment Type", "Full time", "Department", "Operations"):
        assert noise not in posting.location


async def test_a_remote_location_type_is_kept_with_the_location(page) -> None:
    """Ashby states the working mode in its own section.

    `locality.reads_as_remote` reads the location field, and a posting whose
    location names four countries and whose mode says Remote is a different
    job from one that names four offices.
    """
    await page.set_content(
        _ASHBY_DETAILS.replace("United Kingdom; Germany; New York; Poland", "New York")
    )

    posting = await AshbyAdapter().parse_posting(page)

    assert posting.location is not None
    assert "new york" in posting.location.lower()
    assert "remote" in posting.location.lower()


async def test_a_posting_with_no_details_pane_still_parses(page) -> None:
    await page.set_content("<h1>Backend Engineer</h1><div class='_description'>Hi.</div>")

    posting = await AshbyAdapter().parse_posting(page)

    assert posting.title == "Backend Engineer"
    assert posting.location is None


# --- the description is the longest match, not the first ------------------


#: Lever's shape. The posting is split across `.section-wrapper .section`
#: blocks, and the first of the eight is the header.
_LEVER_SECTIONS = """
<div class="posting-header"><h2>Data Scientist - Music Promotion</h2></div>
<div class="location">New York, NY</div>
<div class="content">
  <div class="section-wrapper">
    <div class="section">Data Scientist - Music Promotion New York, NY</div>
    <div class="section" data-qa="job-description">The Music Mission enables Music creators
      to grow their audience, and we are looking for a data scientist to help.</div>
    <div class="section">What you will do: build models, run experiments, and ship them.</div>
    <div class="section">Who you are: experienced with Python, SQL and causal inference.</div>
  </div>
</div>
"""


async def test_the_description_is_the_longest_match_not_the_first(page) -> None:
    """`.first` took the header and reported a 109-character job description.

    Measured on a live Spotify posting: `.section-wrapper .section` matches
    eight elements, the first of which is the title and location. The real
    posting is 4,967 characters. A description that short is not an empty
    string, so nothing looks wrong — the tailorer just has almost nothing to
    work from and the ATS keyword score has almost no vocabulary.
    """
    await page.set_content(_LEVER_SECTIONS)

    posting = await LeverAdapter().parse_posting(page)

    body = posting.description_raw or ""
    assert "causal inference" in body
    assert "run experiments" in body
    assert len(body) > 150


async def test_a_single_match_is_still_read(page) -> None:
    await page.set_content(
        "<h2>Backend Engineer</h2><div class='posting-description'>We are hiring.</div>"
    )

    posting = await LeverAdapter().parse_posting(page)

    assert posting.description_raw == "We are hiring."
