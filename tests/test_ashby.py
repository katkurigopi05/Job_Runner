"""Ashby adapter — CLAUDE.md §8.

Fixtures trimmed from the live DOM of a real Ashby posting. Each of the three
ATSes turned out structurally different in a way no fixture written from memory
would have predicted, and Ashby is the sharpest case: it renders no <form> at
all.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from packages.ats.ashby import AshbyAdapter, is_employer_question, profile_key_for
from packages.ats.base import ManualCompletionRequired, QuestionKind
from packages.ats.registry import adapter_for, detect_ats, supported


@pytest_asyncio.fixture
async def page():
    from apps.worker.browser import ephemeral_page

    async with ephemeral_page() as value:
        yield value


#: Note what is absent: a <form>. Ashby renders the application into the page.
_ASHBY_PAGE = """
<h1>Engineering Manager - EU</h1>
<div>
  <div><label>Name</label><input name="_systemfield_name" type="text" required /></div>
  <div><label>Email</label><input name="_systemfield_email" type="email" required /></div>
  <div><label>Resume</label><input name="_systemfield_resume" type="file" /></div>
  <div><label>Describe the best engineer you have worked with</label>
    <textarea name="f890254e-5bf6-483d-87cb-a8cb9e" aria-required="true"></textarea></div>
  <div><label>Age range</label>
    <input name="aed6672f-7c75-4107-9f71-062481" type="radio" value="under30" />
    <input name="aed6672f-7c75-4107-9f71-062481" type="radio" value="30plus" /></div>
  <div><label>Asian or Asian American</label>
    <input name="Asian or Asian American" type="checkbox" /></div>
</div>
"""


async def test_url_matching_and_registry() -> None:
    url = "https://jobs.ashbyhq.com/ashby/7458d4e9-da2e-47bd-98cb-adfda43d42b2"
    assert AshbyAdapter.matches(url)
    assert not AshbyAdapter.matches("https://jobs.lever.co/ro/f25a6c49-5ed4-4aa0-a5bb-b30e9790")
    assert detect_ats(url) == "ashby"
    assert adapter_for(url).name == "ashby"
    assert supported() == ["greenhouse", "lever", "ashby", "workable"]


async def test_fields_are_found_without_a_form_element(page) -> None:
    """The distinguishing fact about Ashby.

    An adapter that scopes to `form` finds nothing here and reports "no
    application form found" on a page that plainly has one.
    """
    await page.set_content(_ASHBY_PAGE)
    assert await page.locator("form").count() == 0

    questions = await AshbyAdapter().enumerate_fields(page)

    assert len(questions) >= 5
    assert "_systemfield_name" in {q.key for q in questions}


async def test_aria_required_counts_as_required(page) -> None:
    """Ashby marks required with aria-required as well as the attribute.

    Reading only the attribute would let a required question look optional, and
    an optional-looking question is one the owner may skip.
    """
    await page.set_content(_ASHBY_PAGE)
    questions = {q.key: q for q in await AshbyAdapter().enumerate_fields(page)}

    assert questions["f890254e-5bf6-483d-87cb-a8cb9e"].required is True


async def test_employer_questions_carry_their_label_not_their_uuid(page) -> None:
    """§2.4 — a bare uuid is not a question."""
    await page.set_content(_ASHBY_PAGE)
    questions = {q.key: q for q in await AshbyAdapter().enumerate_fields(page)}

    assert (
        questions["f890254e-5bf6-483d-87cb-a8cb9e"].label
        == "Describe the best engineer you have worked with"
    )


async def test_option_groups_collapse_to_one_question(page) -> None:
    await page.set_content(_ASHBY_PAGE)
    questions = await AshbyAdapter().enumerate_fields(page)

    assert sum(1 for q in questions if q.key == "aed6672f-7c75-4107-9f71-062481") == 1
    assert questions[0].kind is not QuestionKind.HIDDEN


async def test_system_fields_map_and_uuids_do_not() -> None:
    assert profile_key_for("_systemfield_email") == "email"
    assert profile_key_for("_systemfield_resume") == "resume"
    assert profile_key_for("f890254e-5bf6-483d-87cb-a8cb9e") is None

    assert is_employer_question("f890254e-5bf6-483d-87cb-a8cb9e")
    assert not is_employer_question("_systemfield_name")


async def test_captcha_stops_enumeration(page) -> None:
    """§2.5 — Ashby mounts one too, confirmed against the live board."""
    await page.set_content(
        _ASHBY_PAGE + '<iframe src="https://www.google.com/recaptcha/api2/anchor"></iframe>'
    )

    with pytest.raises(ManualCompletionRequired):
        await AshbyAdapter().enumerate_fields(page)


async def test_fill_is_not_pretended_to_work(page) -> None:
    await page.set_content(_ASHBY_PAGE)

    with pytest.raises(NotImplementedError):
        await AshbyAdapter().fill(page, {})
