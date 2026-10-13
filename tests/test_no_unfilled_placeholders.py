"""A blank a model left for someone else to fill is not sent.

"Dear [Hiring Manager]," and "at {company}" are what a model writes when it
does not know. §2.4 says an unanswerable question is never left blank, and a
placeholder is a blank with brackets round it. Until 2026-10-12 both checks
that decide what a model may put in front of an employer accepted one:

    rewrite.vet   "Maintained the billing service at [company] …"   accepted
    cover.vet     "… I want to join [company]. …"                   accepted
    cover.vet     "Dear [Hiring Manager],"  /  "Sincerely, [Your Name]"  accepted

The fabrication guard refused a few shapes, and only by accident:
`[Company Name]` because "Name" traces to nothing, `XX%` because "XX" reads as
an acronym. `[company]` is three ordinary lowercase letters and traced to
nothing it had to. The greeting and the sign-off are set aside before the
guard reads a letter at all, which is exactly where these two usually are.

The question is the one `tests/test_placeholder_integrity.py` in
MadsLorentzen/ai-job-search (MIT) asks of its templates: does a placeholder
survive into the document that is sent.
"""

from __future__ import annotations

import pytest

from packages.tailor.cover import vet as vet_letter
from packages.tailor.guard import SourceCorpus
from packages.tailor.placeholders import unfilled
from packages.tailor.rewrite import vet as vet_bullet

RESUME = """Jane Doe — Backend Engineer

Acme Corp — Senior Backend Engineer, 2021 to 2024
Maintained the billing service and reduced invoice errors by 30%.
Built a Python and PostgreSQL pipeline handling 12 million events a day.
Open-sourced the ingest path as a library [GitHub]

Skills: Python, PostgreSQL, Docker, Kafka
"""
BULLET = "Maintained the billing service and reduced invoice errors by 30%."
LETTER = (
    "Jane Doe, backend engineer. "
    + (
        "The billing service work at Acme Corp is the closest match to this role. "
        "I maintained that service and reduced invoice errors by 30%. "
        "I built a Python and PostgreSQL pipeline handling 12 million events a day. "
    )
    * 4
)


def corpus() -> SourceCorpus:
    return SourceCorpus.from_texts(RESUME)


# --------------------------------------------------------------------------
# What counts as one
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, found",
    [
        ("I want to join [Company Name].", ["[Company Name]"]),
        ("I want to join [company].", ["[company]"]),
        ("Dear [Hiring Manager],", ["[Hiring Manager]"]),
        ("[insert relevant experience here]", ["[insert relevant experience here]"]),
        ("I want to join {company}.", ["{company}"]),
        ("I want to join {{company_name}}.", ["{{company_name}}"]),
        ("I want to join <company>.", ["<company>"]),
        ("Sincerely, <your name>", ["<your name>"]),
        ("Reduced invoice errors by [X]%.", ["[X]"]),
        ("Reduced invoice errors by XX%.", ["XX"]),
        ("Saved $XXX a month.", ["XXX"]),
    ],
)
def test_a_blank_left_to_fill_is_found(text: str, found: list[str]) -> None:
    assert unfilled(text) == found


@pytest.mark.parametrize(
    "text",
    [
        "Built the desktop client (Pillow/Tkinter) for the annotation team.",
        "Reach me at <jane@example.com> or (555) 123-4567.",
        "Held p95 latency < 100 ms and uptime > 99.9% for a year.",
        "Wrote C++ and C# services; indexed with a[0] and b[1].",
        "Cited in the review [1] and again in [12].",
        "Wore size XXL to the offsite.",
        "",
    ],
)
def test_ordinary_punctuation_is_not_one(text: str) -> None:
    assert unfilled(text) == []


def test_what_the_owner_wrote_themselves_is_not_one() -> None:
    """Their résumé says `[GitHub]` beside a project. That is their text."""
    line = "Open-sourced the ingest path as a library [GitHub]"

    assert unfilled(line) == ["[GitHub]"]
    assert unfilled(line, known=RESUME) == []


# --------------------------------------------------------------------------
# A résumé bullet
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rewrite",
    [
        "Maintained the billing service at [company] and reduced invoice errors by 30%.",
        "Maintained the billing service in {technology} and reduced invoice errors by 30%.",
        "Maintained the billing service for <team> and reduced invoice errors by 30%.",
        "Maintained the billing service and reduced invoice errors by [X]%.",
    ],
)
def test_a_bullet_with_a_blank_in_it_is_refused(rewrite: str) -> None:
    accepted, reason, _ = vet_bullet(BULLET, rewrite, corpus())

    assert not accepted
    assert reason is not None and "blank" in reason


def test_a_bullet_keeps_the_brackets_its_source_had() -> None:
    original = "Open-sourced the ingest path as a library [GitHub]"

    accepted, reason, _ = vet_bullet(
        original, "Open-sourced the ingest path as a library [GitHub]", corpus()
    )

    assert accepted, reason


def test_an_ordinary_rewrite_is_still_accepted() -> None:
    accepted, reason, _ = vet_bullet(
        BULLET, "Maintained the billing service, reducing invoice errors by 30%.", corpus()
    )

    assert accepted, reason


# --------------------------------------------------------------------------
# A cover letter, greeting and sign-off included
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "letter",
    [
        LETTER.replace("Jane Doe,", "Jane Doe. I want to join [company],", 1),
        LETTER.replace("Jane Doe,", "Jane Doe. I want to join {company},", 1),
        LETTER.replace("Jane Doe,", "Jane Doe. I want to join <company>,", 1),
        "Dear [Hiring Manager],\n\n" + LETTER,
        LETTER + "\n\nSincerely,\n[Your Name]",
    ],
)
def test_a_letter_with_a_blank_in_it_is_refused(letter: str) -> None:
    accepted, reason, _ = vet_letter(letter, corpus())

    assert not accepted
    assert reason is not None and "blank" in reason


def test_a_letter_without_one_is_still_accepted() -> None:
    accepted, reason, _ = vet_letter(LETTER, corpus())
    assert accepted, reason

    accepted, reason, _ = vet_letter("Dear Hiring Manager,\n\n" + LETTER, corpus())
    assert accepted, reason
