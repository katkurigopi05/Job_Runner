"""Filling a form and submitting it, once, for every adapter.

Greenhouse had a `fill` and a `submit`; Lever, Ashby and Workable raised
`NotImplementedError` from both. The refusal was the right call at the time —
an unverified fill path puts unchecked values on a real application — but the
work it was deferring turns out to be almost entirely *not* ATS-specific. What
differs between the four is how you find the form and what the fields are
called, and `enumerate_fields` has already answered both by the time anything
gets filled.

So the mechanics live here and each adapter passes its own `SELECTORS`. One
place to fix a bug in, rather than four that drift — which is the same argument
§10 makes for keeping selectors in a dict at the top of each adapter.

Two rules in here carry the product's guarantees rather than its convenience:

- **A control that cannot be set is never quietly dropped.** If the question
  was required it is parked with the employer's own wording (§2.4); only an
  optional one is skipped. Skipping a required field would leave
  `FillReport.is_complete` true for a form with a hole in it.
- **A dropdown answer has to match an option the employer offered.** Typing
  text into a combobox selects nothing, and §2.2 makes that the difference
  between a work-authorization answer that is recorded and one that silently
  is not.
"""

from __future__ import annotations

from typing import Any

import structlog

from packages.ats.base import (
    FilledField,
    FillReport,
    ManualCompletionRequired,
    Question,
    QuestionKind,
    Receipt,
    SiteError,
    SkippedField,
    UnansweredQuestion,
)

log = structlog.get_logger(__name__)

#: A field that will not accept a value must fail fast. The default page
#: timeout is 30s, and a disabled control would hold the task's lease for it.
FIELD_ACTION_TIMEOUT_MS = 5_000

#: How long to wait for a react-select menu to render after opening it. React
#: does not mount inside the click handler.
MENU_OPEN_TIMEOUT_MS = 3_000

#: Fallbacks for adapters whose SELECTORS carry no combobox entries. Only
#: Greenhouse renders dropdowns this way; the roles are from the ARIA spec, so
#: they are the right guess for any site that starts.
_DEFAULT_LISTBOX = '[role="listbox"]'
_DEFAULT_OPTION = '[role="option"]'

#: Nothing to fill and nothing to ask about.
_INERT_KINDS = (QuestionKind.HIDDEN, QuestionKind.DISPLAY)


class OptionNotOffered(Exception):
    """The answer is not one of the choices the employer listed.

    Never a reason to type it in anyway: the whole point of a select is that
    the site decides what the valid answers are.
    """


def field_selector(element_id: str | None, name: str | None) -> str | None:
    """A selector that finds this control again.

    Attribute form rather than `#id`, because these names are not CSS
    identifiers: Lever's are `cards[<uuid>][question]` and Ashby's are bare
    uuids, both of which a `#` selector reads as something else entirely.
    """
    for attribute, value in (("id", element_id), ("name", name)):
        if value:
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            return f'[{attribute}="{escaped}"]'
    return None


async def fill_form(
    adapter: Any,
    page: Any,
    answers: dict[str, Any],
    *,
    selectors: dict[str, str],
) -> FillReport:
    """Fill what there are answers for. Never invent one.

    A required question with no answer goes into `unanswered` carrying its
    exact text, which is what parks the application for the owner (§2.4).
    """
    await adapter._guard_automation_blocks(page)

    questions = await adapter.enumerate_fields(page)
    scope = page.locator(selectors["form"]).first
    report = FillReport()

    for question in questions:
        if question.kind in _INERT_KINDS:
            continue

        if question.key not in answers or answers[question.key] in (None, ""):
            _record_no_answer(report, question)
            continue

        try:
            await set_value(page, scope, question, answers[question.key], selectors=selectors)
        except ManualCompletionRequired:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad field must not abort the rest
            log.warning(
                "field_fill_failed",
                ats=adapter.name,
                key=question.key,
                kind=question.kind.value,
                error=type(exc).__name__,
            )
            _record_failure(report, question, type(exc).__name__)
            continue

        report.filled.append(
            FilledField(
                key=question.key,
                label=question.label,
                kind=question.kind,
                # §10 — a file's contents never reach the report.
                value=None if question.kind is QuestionKind.FILE else str(answers[question.key]),
            )
        )

    return report


def _record_no_answer(report: FillReport, question: Question) -> None:
    if question.required:
        report.unanswered.append(
            UnansweredQuestion(
                key=question.key,
                question=question.label,
                kind=question.kind,
                options=question.options,
                required=True,
            )
        )
    else:
        report.skipped.append(
            SkippedField(
                key=question.key,
                label=question.label,
                reason="no answer in profile and field is optional",
            )
        )


def _record_failure(report: FillReport, question: Question, error: str) -> None:
    """A field we could not set.

    Parked rather than skipped when it was required. Skipping one would leave
    `is_complete` true for a form with a hole in it, and the owner would be
    told an application was ready to send when a required answer is missing.
    """
    if question.required:
        report.unanswered.append(
            UnansweredQuestion(
                key=question.key,
                question=question.label,
                kind=question.kind,
                options=question.options,
                required=True,
            )
        )
        return
    report.skipped.append(
        SkippedField(key=question.key, label=question.label, reason=f"could not fill: {error}")
    )


async def set_value(
    page: Any,
    scope: Any,
    question: Question,
    value: Any,
    *,
    selectors: dict[str, str],
) -> None:
    """Put one answer on one control, the way that control takes answers."""
    selector = question.selector or field_selector(None, question.key)
    if selector is None:  # pragma: no cover - enumerate always names a field
        raise SiteError("field has no selector to fill by")

    locator = scope.locator(selector).first

    match question.kind:
        case QuestionKind.FILE:
            await locator.set_input_files(str(value), timeout=FIELD_ACTION_TIMEOUT_MS)

        case QuestionKind.SINGLE_SELECT | QuestionKind.MULTI_SELECT:
            await _choose(page, locator, question, value, selectors=selectors)

        case QuestionKind.CHECKBOX | QuestionKind.BOOLEAN:
            if bool(value):
                await locator.check(timeout=FIELD_ACTION_TIMEOUT_MS)
            else:
                await locator.uncheck(timeout=FIELD_ACTION_TIMEOUT_MS)

        case QuestionKind.RADIO:
            # The group shares a name; the value is what picks one of them.
            escaped = str(value).replace('"', '\\"')
            option = scope.locator(f'{selector}[value="{escaped}"]').first
            if not await option.count():
                raise OptionNotOffered(f"{question.key} has no option with that value")
            await option.check(timeout=FIELD_ACTION_TIMEOUT_MS)

        case _:
            await locator.fill(str(value), timeout=FIELD_ACTION_TIMEOUT_MS)


async def _choose(
    page: Any,
    locator: Any,
    question: Question,
    value: Any,
    *,
    selectors: dict[str, str],
) -> None:
    """Select an option, on a real `<select>` or on a react-select combobox.

    Greenhouse has no `<select>` elements at all — every dropdown is an
    `input type="text"` with `role="combobox"`, and `select_option` cannot
    touch one. Which it is has to be read off the page rather than assumed per
    ATS: an adapter that guessed would be wrong the first time a site changed
    its widget, and §2.2 makes that failure silent.
    """
    tag = (await locator.evaluate("el => el.tagName")).lower()

    if tag == "select":
        wanted = value if isinstance(value, list) else [str(value)]
        await locator.select_option(wanted, timeout=FIELD_ACTION_TIMEOUT_MS)
        return

    await _choose_from_menu(page, locator, question, value, selectors=selectors)


async def _choose_from_menu(
    page: Any,
    locator: Any,
    question: Question,
    value: Any,
    *,
    selectors: dict[str, str],
) -> None:
    """Open a combobox and click the option that matches.

    Matched on the option's own label or `data-value`, never on a prefix: "Yes"
    and "Yes, with sponsorship" are different answers to a work-authorization
    question, and §2.2 requires the one the owner actually gave.
    """
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    listbox = selectors.get("react_select_listbox", _DEFAULT_LISTBOX)
    option_selector = selectors.get("react_select_option", _DEFAULT_OPTION)

    await locator.click(timeout=FIELD_ACTION_TIMEOUT_MS)
    try:
        menu = page.locator(listbox).first
        try:
            await menu.wait_for(state="attached", timeout=MENU_OPEN_TIMEOUT_MS)
        except PlaywrightTimeout:
            raise OptionNotOffered(f"{question.key} did not open a menu") from None

        nodes = menu.locator(option_selector)
        wanted = str(value).strip().casefold()

        for index in range(await nodes.count()):
            node = nodes.nth(index)
            label = " ".join((await node.inner_text()).split()).casefold()
            data = (await node.get_attribute("data-value") or "").casefold()
            if wanted in (label, data):
                await node.click(timeout=FIELD_ACTION_TIMEOUT_MS)
                return

        raise OptionNotOffered(f"{question.key} offers no option matching the answer")
    finally:
        if await page.locator(listbox).count():
            # Escape closes the menu without selecting anything or submitting.
            await locator.press("Escape")


async def submit_form(adapter: Any, page: Any, *, selectors: dict[str, str]) -> Receipt:
    """Click submit and record what the site said back.

    Only ever reached after the approval gate — see apps/worker/apply_job.py.
    """
    await adapter._guard_automation_blocks(page)

    button = page.locator(selectors["submit_button"]).first
    if not await button.count():
        # Never report a submission that did not happen.
        raise SiteError("no submit button found on application form")

    await button.click()
    await page.wait_for_load_state("networkidle")

    confirmation = None
    confirm_locator = page.locator(selectors["confirmation"]).first
    if await confirm_locator.count():
        confirmation = " ".join((await confirm_locator.inner_text()).split())

    return Receipt(
        submitted=True,
        ats=adapter.name,
        url=page.url,
        confirmation_text=confirmation,
    )
