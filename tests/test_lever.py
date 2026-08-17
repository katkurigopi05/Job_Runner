"""Lever adapter — CLAUDE.md §8.

Fixtures are trimmed from the live DOM of a real Lever posting, not written
from memory. The Greenhouse adapter classified every dropdown as free text for
weeks because its fixture was hand-written and the real board had moved on.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from packages.ats.base import ManualCompletionRequired, QuestionKind
from packages.ats.lever import LeverAdapter, profile_key_for
from packages.ats.registry import adapter_for, detect_ats, supported


@pytest_asyncio.fixture
async def page():
    from apps.worker.browser import ephemeral_page

    async with ephemeral_page() as value:
        yield value


#: Trimmed from jobs.lever.co. Native <select>, bracketed url fields, and
#: cards[<uuid>] questions whose names say nothing.
_LEVER_FORM = """
<div class="posting-header"><h2>Compounding Pharmacy Technician - Romeoville</h2></div>
<div class="location">Romeoville, IL</div>
<form id="application-form">
  <li class="application-field"><label class="application-label">Resume</label>
    <input name="resume" type="file" /></li>
  <li class="application-field"><label class="application-label">Full name</label>
    <input name="name" type="text" required /></li>
  <li class="application-field"><label class="application-label">Email</label>
    <input name="email" type="email" required /></li>
  <li class="application-field"><label class="application-label">Current company</label>
    <input name="org" type="text" required /></li>
  <li class="application-field"><label class="application-label">LinkedIn URL</label>
    <input name="urls[LinkedIn]" type="text" /></li>
  <li class="application-question"><label class="application-label">Pronouns</label>
    <input name="pronouns" type="checkbox" value="he" />
    <input name="pronouns" type="checkbox" value="she" />
    <input name="pronouns" type="checkbox" value="they" /></li>
  <li class="application-question"><label class="application-label">Shift preference</label>
    <select name="cards[06e54799][shift]">
      <option value="">Select...</option>
      <option value="day">Day</option>
      <option value="night">Night</option>
    </select></li>
  <li class="application-question">
    <label class="application-label">Are you legally authorized to work in the US?</label>
    <input name="cards[e6a8b66e][auth]" type="radio" value="yes" required />
    <input name="cards[e6a8b66e][auth]" type="radio" value="no" required /></li>
</form>
"""


async def test_url_matching() -> None:
    assert LeverAdapter.matches("https://jobs.lever.co/ro/f25a6c49-5ed4-4aa0-a5bb-b30e9790f90c")
    assert LeverAdapter.matches("https://jobs.eu.lever.co/acme/abc123def4567890/apply")
    assert not LeverAdapter.matches("https://boards.greenhouse.io/acme/jobs/1")
    assert not LeverAdapter.matches("https://example.com/careers")


async def test_registry_routes_lever() -> None:
    """§8 — one import and one list entry, and detection follows."""
    url = "https://jobs.lever.co/ro/f25a6c49-5ed4-4aa0-a5bb-b30e9790f90c"
    assert detect_ats(url) == "lever"
    assert adapter_for(url).name == "lever"
    assert "lever" in supported()


async def test_parses_the_posting(page) -> None:
    await page.set_content(_LEVER_FORM)
    posting = await LeverAdapter().parse_posting(page)

    assert posting.title == "Compounding Pharmacy Technician - Romeoville"
    assert posting.location == "Romeoville, IL"


async def test_native_selects_are_dropdowns_with_options(page) -> None:
    """Lever uses real <select>, unlike Greenhouse's react-select."""
    await page.set_content(_LEVER_FORM)
    questions = {q.key: q for q in await LeverAdapter().enumerate_fields(page)}

    shift = questions["cards[06e54799][shift]"]
    assert shift.kind is QuestionKind.SINGLE_SELECT
    assert [o.value for o in shift.options] == ["day", "night"]


async def test_option_groups_are_one_question(page) -> None:
    """One name repeated across options is one question, not one per option."""
    await page.set_content(_LEVER_FORM)
    questions = await LeverAdapter().enumerate_fields(page)

    assert sum(1 for q in questions if q.key == "pronouns") == 1
    assert sum(1 for q in questions if q.key == "cards[e6a8b66e][auth]") == 1


async def test_card_questions_carry_the_employers_wording(page) -> None:
    """§2.4 — a cards[<uuid>] name describes nothing; the label is the question."""
    await page.set_content(_LEVER_FORM)
    questions = {q.key: q for q in await LeverAdapter().enumerate_fields(page)}

    assert (
        questions["cards[e6a8b66e][auth]"].label == "Are you legally authorized to work in the US?"
    )


async def test_core_fields_map_to_profile_keys_and_cards_do_not() -> None:
    """Guessing what a uuid means would be inventing an answer."""
    assert profile_key_for("email") == "email"
    assert profile_key_for("urls[LinkedIn]") == "linkedin"
    assert profile_key_for("org") == "current_company"
    assert profile_key_for("cards[e6a8b66e][auth]") is None


async def test_captcha_stops_enumeration(page) -> None:
    """§2.5 — Lever mounts one on its apply forms. Stop, never route around."""
    await page.set_content(
        _LEVER_FORM + '<iframe src="https://www.google.com/recaptcha/api2/anchor"></iframe>'
    )

    with pytest.raises(ManualCompletionRequired):
        await LeverAdapter().enumerate_fields(page)


async def test_fill_is_not_pretended_to_work(page) -> None:
    """An unverified fill path would put unchecked values on a real application."""
    await page.set_content(_LEVER_FORM)

    with pytest.raises(NotImplementedError):
        await LeverAdapter().fill(page, {})
