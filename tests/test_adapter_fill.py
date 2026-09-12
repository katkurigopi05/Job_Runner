"""Filling and submitting, held to one contract across all four adapters.

Lever, Ashby and Workable raised `NotImplementedError` from `fill` and
`submit`. The refusal was honest — an unverified fill path puts unchecked
values on a real application — but it meant three of the four ATSes could
parse a posting, read the questions, and then do nothing with them.

Each fixture is that ATS's own shape, because the shapes are genuinely
different and a shared fixture would prove nothing:

- Greenhouse: ids, and every dropdown is react-select with no `<select>`.
- Lever: `name=` attributes, native selects, `cards[<uuid>][...]` questions.
- Ashby: no `<form>` element at all, `_systemfield_` prefixes.
- Workable: labels wrapping controls, `QA_<id>` employer questions.

The contract is the same for all of them, which is the point of §8.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from packages.ats.ashby import AshbyAdapter
from packages.ats.base import ManualCompletionRequired, QuestionKind, SiteError
from packages.ats.greenhouse import GreenhouseAdapter
from packages.ats.lever import LeverAdapter
from packages.ats.workable import WorkableAdapter


@pytest_asyncio.fixture
async def page():
    from apps.worker.browser import ephemeral_page

    async with ephemeral_page() as value:
        yield value


_GREENHOUSE = """
<form id="application_form">
  <label for="first_name">First Name *</label>
  <input id="first_name" name="first_name" type="text" required />
  <label for="phone">Phone</label>
  <input id="phone" name="phone" type="tel" />
  <label for="resume">Resume *</label>
  <input id="resume" name="resume" type="file" required />
  <label for="cover_letter_text">Cover Letter</label>
  <textarea id="cover_letter_text" name="cover_letter_text"></textarea>
  <label for="q_dropdown">Preferred shift</label>
  <select id="q_dropdown" name="q_dropdown">
    <option value="">Select...</option><option value="day">Day</option>
    <option value="night">Night</option>
  </select>
  <fieldset><legend>Are you legally authorized to work in the United States? *</legend>
    <input id="q_auth" name="q_auth" type="radio" value="yes" required />
    <input name="q_auth" type="radio" value="no" required /></fieldset>
  <label for="q_terms">I agree to the terms</label>
  <input id="q_terms" name="q_terms" type="checkbox" />
  <label for="q_open">Why do you want to work at Acme? *</label>
  <textarea id="q_open" name="q_open" required></textarea>
  <button type="submit">Submit Application</button>
</form>
"""

_LEVER = """
<form id="application-form">
  <li class="application-field"><label class="application-label">Full name</label>
    <input name="first_name" type="text" required /></li>
  <li class="application-field"><label class="application-label">Phone</label>
    <input name="phone" type="tel" /></li>
  <li class="application-field"><label class="application-label">Resume</label>
    <input name="resume" type="file" required /></li>
  <li class="application-field"><label class="application-label">Cover Letter</label>
    <textarea name="cover_letter_text"></textarea></li>
  <li class="application-question"><label class="application-label">Preferred shift</label>
    <select name="q_dropdown">
      <option value="">Select...</option><option value="day">Day</option>
      <option value="night">Night</option></select></li>
  <li class="application-question">
    <label class="application-label">
      Are you legally authorized to work in the United States?</label>
    <input name="q_auth" type="radio" value="yes" required />
    <input name="q_auth" type="radio" value="no" required /></li>
  <li class="application-question"><label class="application-label">I agree to the terms</label>
    <input name="q_terms" type="checkbox" value="yes" /></li>
  <li class="application-question">
    <label class="application-label">Why do you want to work at Acme?</label>
    <textarea name="q_open" required></textarea></li>
  <button type="submit" class="template-btn-submit">Submit application</button>
</form>
"""

_ASHBY = """
<body><div class="_fieldEntry"><label>Full name</label>
  <input name="first_name" type="text" aria-required="true" /></div>
<div class="_fieldEntry"><label>Phone</label><input name="phone" type="tel" /></div>
<div class="_fieldEntry"><label>Resume</label>
  <input name="resume" type="file" aria-required="true" /></div>
<div class="_fieldEntry"><label>Cover Letter</label>
  <textarea name="cover_letter_text"></textarea></div>
<div class="_fieldEntry"><label>Preferred shift</label>
  <select name="q_dropdown"><option value="">Select...</option>
    <option value="day">Day</option><option value="night">Night</option></select></div>
<div class="_fieldEntry">
  <label>Are you legally authorized to work in the United States?</label>
  <input name="q_auth" type="radio" value="yes" aria-required="true" />
  <input name="q_auth" type="radio" value="no" aria-required="true" /></div>
<div class="_fieldEntry"><label>I agree to the terms</label>
  <input name="q_terms" type="checkbox" value="yes" /></div>
<div class="_fieldEntry"><label>Why do you want to work at Acme?</label>
  <textarea name="q_open" aria-required="true"></textarea></div>
<button type="submit">Submit Application</button></body>
"""

_WORKABLE = """
<body><form class="styles--2I-rr">
  <label>Full name<input name="first_name" type="text" required /></label>
  <label>Phone<input name="phone" type="tel" /></label>
  <label>Resume<input name="resume" type="file" required /></label>
  <label>Cover Letter<textarea name="cover_letter_text"></textarea></label>
  <label>Preferred shift<select name="q_dropdown"><option value="">Select...</option>
    <option value="day">Day</option><option value="night">Night</option></select></label>
  <label>Are you legally authorized to work in the United States?
    <input name="q_auth" type="radio" value="yes" required />
    <input name="q_auth" type="radio" value="no" required /></label>
  <label>I agree to the terms<input name="q_terms" type="checkbox" value="yes" /></label>
  <label>Why do you want to work at Acme?<textarea name="q_open" required></textarea></label>
  <button type="submit">Submit</button>
</form></body>
"""

ADAPTERS = [
    pytest.param(GreenhouseAdapter, _GREENHOUSE, id="greenhouse"),
    pytest.param(LeverAdapter, _LEVER, id="lever"),
    pytest.param(AshbyAdapter, _ASHBY, id="ashby"),
    pytest.param(WorkableAdapter, _WORKABLE, id="workable"),
]

#: Every answer the fixtures can take. `q_open` is deliberately absent: it is
#: the required question nothing in a profile answers, and §2.4 parks it.
ANSWERS = {
    "first_name": "Ada Lovelace",
    "phone": "+1 415 555 0123",
    "cover_letter_text": "I have wanted to work at Acme since 1843.",
    "q_dropdown": "night",
    "q_auth": "yes",
    "q_terms": True,
}


@pytest.mark.parametrize(("adapter_cls", "html"), ADAPTERS)
async def test_text_answers_land_in_the_form(page, adapter_cls, html) -> None:
    await page.set_content(html)

    report = await adapter_cls().fill(page, ANSWERS)

    assert await page.input_value('[name="first_name"]') == "Ada Lovelace"
    assert await page.input_value('[name="phone"]') == "+1 415 555 0123"
    assert {f.key for f in report.filled} >= {"first_name", "phone"}


@pytest.mark.parametrize(("adapter_cls", "html"), ADAPTERS)
async def test_the_cover_letter_is_typed_into_its_textarea(page, adapter_cls, html) -> None:
    """A letter written, vetted, stored and then not typed is the résumé defect."""
    await page.set_content(html)

    await adapter_cls().fill(page, ANSWERS)

    assert await page.input_value('[name="cover_letter_text"]') == ANSWERS["cover_letter_text"]


@pytest.mark.parametrize(("adapter_cls", "html"), ADAPTERS)
async def test_a_dropdown_is_selected_not_typed_into(page, adapter_cls, html) -> None:
    """§2.2 — an answer that must match an offered option has to select one."""
    await page.set_content(html)

    await adapter_cls().fill(page, ANSWERS)

    assert await page.input_value('[name="q_dropdown"]') == "night"


@pytest.mark.parametrize(("adapter_cls", "html"), ADAPTERS)
async def test_a_radio_group_picks_the_offered_value(page, adapter_cls, html) -> None:
    await page.set_content(html)

    await adapter_cls().fill(page, ANSWERS)

    assert await page.is_checked('[name="q_auth"][value="yes"]')
    assert not await page.is_checked('[name="q_auth"][value="no"]')


@pytest.mark.parametrize(("adapter_cls", "html"), ADAPTERS)
async def test_a_checkbox_is_checked(page, adapter_cls, html) -> None:
    await page.set_content(html)

    await adapter_cls().fill(page, ANSWERS)

    assert await page.is_checked('[name="q_terms"]')


@pytest.mark.parametrize(("adapter_cls", "html"), ADAPTERS)
async def test_the_resume_file_is_attached(page, adapter_cls, html, tmp_path) -> None:
    await page.set_content(html)
    resume = tmp_path / "ada.pdf"
    resume.write_bytes(b"%PDF-1.4 fake")

    report = await adapter_cls().fill(page, {**ANSWERS, "resume": str(resume)})

    attached = await page.evaluate(
        """() => document.querySelector('[name="resume"]').files[0]?.name"""
    )
    assert attached == "ada.pdf"
    # §10 — a file's contents never reach the report.
    (field,) = [f for f in report.filled if f.key == "resume"]
    assert field.value is None


@pytest.mark.parametrize(("adapter_cls", "html"), ADAPTERS)
async def test_an_unanswered_required_question_carries_the_employers_words(
    page, adapter_cls, html
) -> None:
    """§2.4 — never guess, never blank; surface the question as asked."""
    await page.set_content(html)

    report = await adapter_cls().fill(page, ANSWERS)

    assert "Why do you want to work at Acme?" in {q.question for q in report.unanswered}
    assert not report.is_complete


@pytest.mark.parametrize(("adapter_cls", "html"), ADAPTERS)
async def test_an_unanswered_optional_field_is_skipped_not_parked(page, adapter_cls, html) -> None:
    """Parking on an optional field would ask the owner to do nothing."""
    await page.set_content(html)

    answers = {k: v for k, v in ANSWERS.items() if k != "phone"}
    report = await adapter_cls().fill(page, answers)

    assert "phone" in {f.key for f in report.skipped}
    assert "phone" not in {q.key for q in report.unanswered}


@pytest.mark.parametrize(("adapter_cls", "html"), ADAPTERS)
async def test_a_complete_fill_reports_complete(page, adapter_cls, html, tmp_path) -> None:
    await page.set_content(html)
    resume = tmp_path / "ada.pdf"
    resume.write_bytes(b"%PDF-1.4 fake")

    report = await adapter_cls().fill(
        page,
        {**ANSWERS, "resume": str(resume), "q_open": "Because of the Analytical Engine."},
    )

    assert report.is_complete
    assert report.unanswered == []


# --- submitting -----------------------------------------------------------


_CONFIRMATION = """
<script>
  document.addEventListener('click', function (event) {
    if (event.target.type !== 'submit') return;
    event.preventDefault();
    document.body.innerHTML =
      '<div class="application-confirmation">Thank you for applying to Acme.</div>';
  });
</script>
"""


@pytest.mark.parametrize(("adapter_cls", "html"), ADAPTERS)
async def test_submit_clicks_and_reports_what_the_site_said(page, adapter_cls, html) -> None:
    await page.set_content(html + _CONFIRMATION)

    receipt = await adapter_cls().submit(page)

    assert receipt.submitted is True
    assert receipt.ats == adapter_cls.name
    assert receipt.confirmation_text == "Thank you for applying to Acme."


@pytest.mark.parametrize(("adapter_cls", "html"), ADAPTERS)
async def test_submit_without_a_button_is_a_site_error(page, adapter_cls, html) -> None:
    """Never report a submission that did not happen."""
    import re

    # The whole element, class attribute included: Lever also recognises its
    # own `.template-btn-submit`, so renaming the tag alone leaves a button.
    await page.set_content(re.sub(r"<button[^>]*>.*?</button>", "", html, flags=re.S))

    with pytest.raises(SiteError):
        await adapter_cls().submit(page)


# --- the scope boundary ---------------------------------------------------


_CAPTCHA = '<div class="g-recaptcha"></div>'


@pytest.mark.parametrize(("adapter_cls", "html"), ADAPTERS)
async def test_a_captcha_stops_the_fill(page, adapter_cls, html) -> None:
    """§2.5 — a blocked site is a scope boundary, not a gap to close."""
    await page.set_content(html + _CAPTCHA)

    with pytest.raises(ManualCompletionRequired):
        await adapter_cls().fill(page, ANSWERS)


@pytest.mark.parametrize(("adapter_cls", "html"), ADAPTERS)
async def test_a_captcha_stops_the_submit(page, adapter_cls, html) -> None:
    await page.set_content(html + _CAPTCHA)

    with pytest.raises(ManualCompletionRequired):
        await adapter_cls().submit(page)


@pytest.mark.parametrize(("adapter_cls", "html"), ADAPTERS)
async def test_one_bad_field_does_not_abort_the_rest(page, adapter_cls, html) -> None:
    """A single unfillable control must not cost every other answer."""
    await page.set_content(html.replace('name="phone"', 'name="phone" disabled'))

    report = await adapter_cls().fill(page, ANSWERS)

    assert "phone" in {f.key for f in report.skipped}
    assert await page.input_value('[name="first_name"]') == "Ada Lovelace"


# --- react-select, which only Greenhouse has ------------------------------


_REACT_SELECT = """
<form id="application_form">
  <label id="q1-label" for="q1" class="label select__label">
    Are you authorized to work in the country for which you applied?</label>
  <input class="select__input" id="q1" name="q1" type="text" role="combobox"
         aria-haspopup="true" aria-autocomplete="list" aria-required="true" />
  <input id="chosen" name="chosen" type="hidden" />
</form>
<script>
  const control = document.querySelector("#q1");
  control.addEventListener("click", () => {
    if (document.querySelector("#react-select-2-listbox")) return;
    document.body.insertAdjacentHTML("beforeend",
      `<div id="react-select-2-listbox" role="listbox">
         <div role="option" data-value="yes">Yes</div>
         <div role="option" data-value="no">No</div>
       </div>`);
    document.querySelectorAll('#react-select-2-listbox [role="option"]').forEach((node) => {
      node.addEventListener("click", () => {
        control.value = node.textContent.trim();
        document.querySelector("#chosen").value = node.dataset.value;
        document.querySelector("#react-select-2-listbox").remove();
      });
    });
  });
  control.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    document.querySelector("#react-select-2-listbox")?.remove();
  });
</script>
"""


async def test_a_react_select_answer_is_chosen_from_the_menu(page) -> None:
    """Typing into a combobox selects nothing, and the answer never lands.

    Greenhouse renders every dropdown this way — there is no `<select>` on the
    page — so `select_option` cannot work here. §2.2 makes this the difference
    between a work-authorization answer that is recorded and one that silently
    is not.
    """
    await page.set_content(_REACT_SELECT)

    report = await GreenhouseAdapter().fill(page, {"q1": "Yes"})

    assert await page.input_value("#chosen") == "yes"
    assert "q1" in {f.key for f in report.filled}


async def test_a_react_select_answer_that_is_not_offered_is_not_invented(page) -> None:
    """An option the employer never offered must not be typed in as free text."""
    await page.set_content(_REACT_SELECT)

    report = await GreenhouseAdapter().fill(page, {"q1": "Maybe"})

    assert await page.input_value("#chosen") == ""
    assert "q1" in {f.key for f in report.skipped}
    assert "q1" not in {f.key for f in report.filled}


async def test_the_dropdown_kind_survives_the_round_trip(page) -> None:
    await page.set_content(_REACT_SELECT)

    (question,) = [q for q in await GreenhouseAdapter().enumerate_fields(page) if q.key == "q1"]

    assert question.kind is QuestionKind.SINGLE_SELECT
