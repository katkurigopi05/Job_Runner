"""Greenhouse adapter — Gate 1, offline half.

These drive a real Chromium against a local fixture of a Greenhouse
application form, so the whole flow (parse → enumerate → fill → screenshot)
runs with no network. Gate 1's other half — the same flow against a live
posting — has to be run by the owner; this sandbox's egress policy blocks
greenhouse.io.

IMPORTANT: the fixture is a reconstruction of Greenhouse's markup, not a
recording of it. These tests prove the adapter is internally consistent and
that its logic is right; they do NOT prove the selectors match today's live
DOM. Only a live run does that.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from packages.ats.base import ManualCompletionRequired, QuestionKind, SiteError
from packages.ats.greenhouse import GreenhouseAdapter
from packages.ats.registry import adapter_for, detect_ats, supported
from packages.core.storage import LocalStorage, set_storage

FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse_posting.html"


@pytest.fixture
async def page():
    from apps.worker.browser import ephemeral_page

    async with ephemeral_page() as p:
        yield p


@pytest.fixture
async def posting_page(page):
    await page.goto(FIXTURE.as_uri())
    return page


# --------------------------------------------------------------------------
# URL detection — pure, no browser
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://boards.greenhouse.io/acme/jobs/4012345",
        "https://job-boards.greenhouse.io/acme/jobs/4012345",
        "https://boards.greenhouse.io/acme/jobs/4012345/application",
        "https://boards.greenhouse.io/embed/job_app?for=acme&token=123",
    ],
)
def test_matches_greenhouse_urls(url: str) -> None:
    assert GreenhouseAdapter.matches(url)
    assert detect_ats(url) == "greenhouse"


@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.lever.co/acme/123",
        "https://acme.com/careers/engineer",
        "https://jobs.ashbyhq.com/acme/123",
        "https://boards.greenhouse.io/acme",  # no job id
    ],
)
def test_rejects_other_urls(url: str) -> None:
    assert not GreenhouseAdapter.matches(url)
    assert detect_ats(url) != "greenhouse"


def test_external_id_and_company() -> None:
    url = "https://boards.greenhouse.io/acme/jobs/4012345"
    assert GreenhouseAdapter.external_id(url) == "4012345"
    assert GreenhouseAdapter.company_slug(url) == "acme"


def test_registry_resolves_adapter() -> None:
    adapter = adapter_for("https://boards.greenhouse.io/acme/jobs/1")
    assert adapter.name == "greenhouse"
    assert "greenhouse" in supported()


def test_unsupported_url_raises() -> None:
    from packages.ats.base import UnsupportedSiteError

    with pytest.raises(UnsupportedSiteError):
        adapter_for("https://acme.com/careers/1")


# --------------------------------------------------------------------------
# parse_posting
# --------------------------------------------------------------------------


async def test_parse_posting_reads_the_page(posting_page) -> None:
    parsed = await GreenhouseAdapter().parse_posting(posting_page)

    assert parsed.title == "Senior Backend Engineer"
    assert parsed.location is not None and "Austin" in parsed.location
    assert parsed.description_raw is not None
    assert "distributed systems" in parsed.description_raw
    assert not parsed.closed


async def test_closed_posting_is_detected(page) -> None:
    await page.set_content(
        "<h1>Engineer</h1><p>This job is closed and no longer accepting applications.</p>"
    )
    parsed = await GreenhouseAdapter().parse_posting(page)
    assert parsed.closed


# --------------------------------------------------------------------------
# enumerate_fields — the real field list comes from the DOM
# --------------------------------------------------------------------------


async def test_enumerate_returns_the_real_field_list(posting_page) -> None:
    questions = await GreenhouseAdapter().enumerate_fields(posting_page)
    keys = [q.key for q in questions]

    assert "first_name" in keys
    assert "last_name" in keys
    assert "email" in keys
    assert "phone" in keys
    assert "resume" in keys
    assert "cover_letter" in keys
    # Custom employer questions are discovered, not hardcoded.
    assert "job_application_answers_attributes_1_boolean_value" in keys


async def test_enumerate_infers_kinds(posting_page) -> None:
    questions = {q.key: q for q in await GreenhouseAdapter().enumerate_fields(posting_page)}

    assert questions["email"].kind is QuestionKind.EMAIL
    assert questions["phone"].kind is QuestionKind.PHONE
    assert questions["resume"].kind is QuestionKind.FILE
    assert questions["cover_letter"].kind is QuestionKind.COVER_LETTER
    assert (
        questions["job_application_answers_attributes_1_boolean_value"].kind
        is QuestionKind.SINGLE_SELECT
    )


async def test_enumerate_captures_exact_question_text(posting_page) -> None:
    """The owner sees the employer's wording, not a paraphrase."""
    questions = {q.key: q for q in await GreenhouseAdapter().enumerate_fields(posting_page)}

    work_auth = questions["job_application_answers_attributes_1_boolean_value"]
    assert work_auth.label == "Are you legally authorized to work in the United States?"

    why = questions["job_application_answers_attributes_2_text_value"]
    assert why.label == "Why do you want to work at Acme?"


async def test_enumerate_marks_required_fields(posting_page) -> None:
    questions = {q.key: q for q in await GreenhouseAdapter().enumerate_fields(posting_page)}

    assert questions["first_name"].required
    assert questions["resume"].required
    assert not questions["phone"].required
    assert not questions["cover_letter"].required


async def test_enumerate_reads_select_options(posting_page) -> None:
    questions = {q.key: q for q in await GreenhouseAdapter().enumerate_fields(posting_page)}
    work_auth = questions["job_application_answers_attributes_1_boolean_value"]

    assert [o.label for o in work_auth.options] == ["Yes", "No"]
    # The empty placeholder is not an answer.
    assert all(o.value for o in work_auth.options)


async def test_enumerate_skips_hidden_fields(posting_page) -> None:
    keys = [q.key for q in await GreenhouseAdapter().enumerate_fields(posting_page)]
    assert "utm_source" not in keys


async def test_missing_form_is_a_site_error(page) -> None:
    await page.set_content("<h1>Job</h1><p>No form here.</p>")
    with pytest.raises(SiteError, match="no application form"):
        await GreenhouseAdapter().enumerate_fields(page)


# --------------------------------------------------------------------------
# Captcha — a hard stop, never an obstacle to route around
# --------------------------------------------------------------------------


async def test_captcha_parks_for_manual_completion(page) -> None:
    await page.set_content(
        "<form id='application_form'>"
        "<div class='g-recaptcha'></div>"
        "<input id='email' type='email'>"
        "</form>"
    )
    with pytest.raises(ManualCompletionRequired):
        await GreenhouseAdapter().enumerate_fields(page)


async def test_captcha_blocks_submit_too(page) -> None:
    await page.set_content(
        "<form id='application_form'>"
        "<iframe src='https://www.google.com/recaptcha/api2/anchor'></iframe>"
        "<input type='submit' id='submit_app'>"
        "</form>"
    )
    with pytest.raises(ManualCompletionRequired):
        await GreenhouseAdapter().submit(page)


# --------------------------------------------------------------------------
# fill
# --------------------------------------------------------------------------


async def test_fill_populates_fields(posting_page) -> None:
    report = await GreenhouseAdapter().fill(
        posting_page,
        {
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@example.com",
            "phone": "+1-555-0100",
        },
    )

    filled = {f.key for f in report.filled}
    assert {"first_name", "last_name", "email", "phone"} <= filled
    assert await posting_page.input_value("#first_name") == "Ada"
    assert await posting_page.input_value("#email") == "ada@example.com"


async def test_fill_selects_an_option(posting_page) -> None:
    key = "job_application_answers_attributes_1_boolean_value"
    report = await GreenhouseAdapter().fill(posting_page, {key: "1"})

    assert key in {f.key for f in report.filled}
    assert await posting_page.input_value(f"#{key}") == "1"


async def test_unanswered_required_question_carries_exact_text(posting_page) -> None:
    """CLAUDE.md §2.4 — never guess, never blank; surface the question."""
    report = await GreenhouseAdapter().fill(posting_page, {"first_name": "Ada"})

    questions = {q.question for q in report.unanswered}
    assert "Why do you want to work at Acme?" in questions
    assert "Are you legally authorized to work in the United States?" in questions
    assert not report.is_complete


async def test_optional_unanswered_fields_are_skipped_not_parked(posting_page) -> None:
    report = await GreenhouseAdapter().fill(posting_page, {})

    skipped = {s.key for s in report.skipped}
    unanswered = {q.key for q in report.unanswered}
    assert "phone" in skipped  # optional
    assert "first_name" in unanswered  # required


async def test_complete_fill_is_marked_complete(posting_page, tmp_path) -> None:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 fake")

    report = await GreenhouseAdapter().fill(
        posting_page,
        {
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@example.com",
            "resume": str(resume),
            "job_application_answers_attributes_1_boolean_value": "1",
            "job_application_answers_attributes_2_text_value": "I admire the work.",
        },
    )

    assert report.is_complete, [q.question for q in report.unanswered]
    assert report.fill_rate > 0.5


async def test_file_contents_are_not_echoed_into_the_report(posting_page, tmp_path) -> None:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 secret contents")

    report = await GreenhouseAdapter().fill(posting_page, {"resume": str(resume)})

    uploaded = next(f for f in report.filled if f.key == "resume")
    assert uploaded.value is None


# --------------------------------------------------------------------------
# Screenshot into storage
# --------------------------------------------------------------------------


async def test_screenshot_lands_in_storage(posting_page, tmp_path) -> None:
    """Gate 1: a screenshot lands in storage/receipts/."""
    storage = LocalStorage(tmp_path)
    set_storage(storage)
    try:
        key = "receipts/test-app/filled-form.png"
        target = storage.path_for(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        await posting_page.screenshot(path=str(target), full_page=True)

        assert storage.exists(key)
        assert storage.get(key)[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        set_storage(None)


# --------------------------------------------------------------------------
# react-select comboboxes — found by gate-1-live against Figma and Vercel
# --------------------------------------------------------------------------

#: Trimmed from the live Figma board. Greenhouse renders every dropdown this
#: way: there is no <select> element on the page at all.
_REACT_SELECT_FORM = """
<form id="application_form">
  <div class="select__container">
    <label id="q1-label" for="q1" class="label select__label">
      Are you authorized to work in the country for which you applied?
    </label>
    <input class="select__input" id="q1" type="text" role="combobox"
           aria-haspopup="true" aria-autocomplete="list" aria-required="true" />
  </div>
  <label for="first_name">First Name</label>
  <input id="first_name" type="text" />
  <div class="iti">
    <input id="iti-0__search-input" type="text" class="iti__search-input" />
  </div>
</form>
<script>
  const control = document.querySelector("#q1");
  control.addEventListener("click", () => {
    if (document.querySelector("#react-select-2-listbox")) return;
    control.setAttribute("aria-expanded", "true");
    control.setAttribute("aria-controls", "react-select-2-listbox");
    document.body.insertAdjacentHTML(
      "beforeend",
      `<div id="react-select-2-listbox" role="listbox">
         <div id="react-select-2-option-0" role="option">Yes</div>
         <div id="react-select-2-option-1" role="option">No</div>
       </div>`,
    );
  });
  control.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    document.querySelector("#react-select-2-listbox")?.remove();
    control.setAttribute("aria-expanded", "false");
  });
</script>
"""


async def test_react_select_is_a_dropdown_not_free_text(page) -> None:
    """§2.2 — a work-authorization answer must match an offered option.

    Greenhouse's dropdowns are `input type="text"` with role="combobox".
    Believing the type attribute classified this as free text, and typing into
    a combobox selects nothing, so the answer silently never landed.
    """
    await page.set_content(_REACT_SELECT_FORM)

    questions = await GreenhouseAdapter().enumerate_fields(page)
    by_key = {q.key: q for q in questions}

    assert by_key["q1"].kind is QuestionKind.SINGLE_SELECT
    # A real text input beside it must stay text.
    assert by_key["first_name"].kind is QuestionKind.TEXT


async def test_enumerate_opens_react_select_and_reads_options(page) -> None:
    """Options only exist while react-select's portal menu is open."""
    await page.set_content(_REACT_SELECT_FORM)
    assert not await page.locator('[role="listbox"]').count()

    questions = {q.key: q for q in await GreenhouseAdapter().enumerate_fields(page)}

    assert [(option.label, option.value) for option in questions["q1"].options] == [
        ("Yes", "Yes"),
        ("No", "No"),
    ]
    assert not await page.locator('[role="listbox"]').count()


async def test_widget_internals_are_not_offered_as_questions(page) -> None:
    """intl-tel-input mounts a country search box inside the form.

    It is not something an employer asked, and surfacing it in the review queue
    invites the owner to answer a question that does not exist.
    """
    await page.set_content(_REACT_SELECT_FORM)

    questions = await GreenhouseAdapter().enumerate_fields(page)

    assert not any(q.key.startswith("iti-") for q in questions)
