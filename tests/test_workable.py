"""Workable adapter tests trimmed from live markup inspected 2026-08-18.

Source posting: https://apply.workable.com/open-252/j/6D0FEF463C/
Application: https://apply.workable.com/open-252/j/6D0FEF463C/apply/

The fixture preserves Workable's structural details: stable ``data-ui``
posting attributes, one unnamed form, labels wrapping controls three levels
up, nameless file inputs, and opaque ``QA_<id>`` employer-question names.
"""

from __future__ import annotations

import pytest

from packages.ats.base import ManualCompletionRequired, QuestionKind, SiteError
from packages.ats.registry import adapter_for, detect_ats, supported
from packages.ats.workable import WorkableAdapter, profile_key_for


@pytest.fixture
async def page():
    from apps.worker.browser import ephemeral_page

    async with ephemeral_page() as browser_page:
        yield browser_page


_LIVE_POSTING_MARKUP = """
<header role="banner">
  <div>
    <a data-ui="company-logo" href="/open-252/"><span>Open</span></a>
    <h1 data-ui="job-title">Product Engineer</h1>
    <p>
      <span data-ui="job-workplace"><strong>Remote</strong></span>
      <span data-ui="job-type">Full time</span>
    </p>
  </div>
</header>
<main role="main">
  <section aria-labelledby="job-description-title" data-ui="job-description">
    <h2 id="job-description-title">Description</h2>
    <div><h3>About Open</h3><p>Build AI-native customer support infrastructure.</p></div>
  </section>
  <section aria-labelledby="job-requirements-title" data-ui="job-requirements">
    <h2 id="job-requirements-title">Requirements</h2>
    <div><p>Three years shipping software products.</p></div>
  </section>
</main>
"""


_LIVE_APPLICATION_MARKUP = """
<h1 data-ui="job-title">Product Engineer</h1>
<form class="styles--2I-rr" action="/open-252/j/6D0FEF463C/apply/" method="get">
  <label class="styles--3aPac">
    *<span>First name</span>
    <div><div><input class="styles--2e9Cp" id="firstname" name="firstname"
      type="text" required aria-required="true"></div></div>
  </label>
  <label class="styles--3aPac">
    *<span>Email</span>
    <div><div><input class="styles--2e9Cp" id="email" name="email"
      type="email" required aria-required="true"></div></div>
  </label>
  <div class="styles--3aPac">
    Resume (Optional)
    <div><input class="styles--1lKzl" id="input_files_input_live" type="file">
      Choose file or drag and drop here</div>
  </div>
  <label class="styles--3aPac">
    *<span>
      What’s one product, tool, or feature you’ve built before that you’re most proud of?
    </span>
    <div><div><textarea class="styles--2e9Cp" id="QA_10337894" name="QA_10337894"
      required aria-required="true"></textarea></div></div>
  </label>
  <label class="styles--3aPac">
    *<span>Your GitHub profile</span>
    <div><div><input class="styles--2e9Cp" id="QA_10337895" name="QA_10337895"
      type="text" required aria-required="true"></div></div>
  </label>
  <button type="submit">Submit application</button>
</form>
"""


@pytest.mark.parametrize(
    "url",
    [
        "https://apply.workable.com/open-252/j/6D0FEF463C/",
        "https://apply.workable.com/open-252/j/6D0FEF463C/apply/",
    ],
)
def test_matches_workable_urls(url: str) -> None:
    assert WorkableAdapter.matches(url)
    assert detect_ats(url) == "workable"


def test_extracts_company_and_posting_id() -> None:
    url = "https://apply.workable.com/open-252/j/6D0FEF463C/apply/"
    assert WorkableAdapter.company_slug(url) == "open-252"
    assert WorkableAdapter.external_id(url) == "6D0FEF463C"


def test_registry_resolves_workable() -> None:
    adapter = adapter_for("https://apply.workable.com/open-252/j/6D0FEF463C/")
    assert adapter.name == "workable"
    assert "workable" in supported()


async def test_parse_posting_reads_live_data_ui_markup(page) -> None:
    await page.set_content(_LIVE_POSTING_MARKUP)

    parsed = await WorkableAdapter().parse_posting(page)

    assert parsed.title == "Product Engineer"
    assert parsed.location == "Remote"
    assert parsed.description_raw is not None
    assert "AI-native customer support" in parsed.description_raw
    assert "Three years shipping" in parsed.description_raw


async def test_enumerates_live_application_markup(page) -> None:
    await page.set_content(_LIVE_APPLICATION_MARKUP)

    questions = {q.key: q for q in await WorkableAdapter().enumerate_fields(page)}

    assert questions["firstname"].kind is QuestionKind.TEXT
    assert questions["email"].kind is QuestionKind.EMAIL
    assert questions["resume"].kind is QuestionKind.FILE
    assert questions["QA_10337894"].kind is QuestionKind.TEXTAREA
    assert questions["firstname"].required


async def test_opaque_employer_question_keeps_exact_label(page) -> None:
    await page.set_content(_LIVE_APPLICATION_MARKUP)

    questions = {q.key: q for q in await WorkableAdapter().enumerate_fields(page)}

    assert questions["QA_10337894"].label == (
        "What’s one product, tool, or feature you’ve built before that you’re most proud of?"
    )
    assert profile_key_for("QA_10337894") is None


async def test_captcha_stops_enumeration(page) -> None:
    await page.set_content(
        '<form action="/company/j/ABC123/apply/">'
        '<iframe src="https://www.google.com/recaptcha/api2/anchor"></iframe>'
        '<input name="email" type="email"></form>'
    )

    with pytest.raises(ManualCompletionRequired):
        await WorkableAdapter().enumerate_fields(page)


async def test_missing_form_is_site_error(page) -> None:
    await page.set_content("<h1 data-ui='job-title'>Product Engineer</h1>")

    with pytest.raises(SiteError, match="no application form"):
        await WorkableAdapter().enumerate_fields(page)


async def test_unverified_write_paths_are_explicit(page) -> None:
    adapter = WorkableAdapter()

    with pytest.raises(NotImplementedError, match="not implemented"):
        await adapter.fill(page, {})
    with pytest.raises(NotImplementedError, match="not implemented"):
        await adapter.submit(page)
