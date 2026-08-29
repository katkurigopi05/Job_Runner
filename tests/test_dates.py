"""What counts as a date on a résumé.

Most of these cases are transcribed from the owner's own certificates, which
is the point: the fixtures written beside the code all used the one shape the
code already handled, and the shapes that broke it only turned up when a real
document did.
"""

from __future__ import annotations

import pytest

from packages.tailor.ats import score
from packages.tailor.dates import contains_date, date_only, is_open_ended, trailing_date
from packages.tailor.parse import parse_text

# --------------------------------------------------------------------------
# Ranges
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "Mar 2021 - Present",
        "Jun 2024 - Aug 2024",
        "2017 - 2021",
        "Jan 2020 to Mar 2021",
        "May 2024 – Present",
    ],
)
def test_a_range_is_a_date(line: str) -> None:
    assert date_only(line) == line


@pytest.mark.parametrize(
    "line",
    [
        "September - November 2023",
        "Sep - Nov 2023",
        "Jul-Aug 2022",
        "Summer - Fall 2024",
    ],
)
def test_a_range_may_write_its_year_once(line: str) -> None:
    """`September - November 2023` — the whole thing happened in one year.

    How a short internship is nearly always dated, and the shape that sent a
    real internship through the renderer as a bold employer name: every other
    pattern wanted a year on both sides of the dash.
    """
    assert date_only(line) == line


# --------------------------------------------------------------------------
# Points — an internship is dated with one
# --------------------------------------------------------------------------


@pytest.mark.parametrize("line", ["Jun 2024", "June 2024", "Summer 2024", "Fall 2023", "2024"])
def test_a_single_moment_is_a_date(line: str) -> None:
    assert date_only(line) == line


@pytest.mark.parametrize(
    "line",
    ["01st Apr, 2021", "15th May, 2021", "12-JUN-2021", "11-Jun-2021", "1 May 2021"],
)
def test_a_day_of_the_month_is_a_date(line: str) -> None:
    """Certificates date things to the day; résumés inherit the format."""
    assert date_only(line) == line


def test_a_day_first_range_is_a_date() -> None:
    line = "01st Apr, 2021 to 15th May, 2021"
    assert date_only(line) == line


def test_repeat_internships_are_one_date() -> None:
    assert date_only("Summer 2023, Summer 2024") == "Summer 2023, Summer 2024"


# --------------------------------------------------------------------------
# Open-ended — the end left off
# --------------------------------------------------------------------------


@pytest.mark.parametrize("line", ["May 2024 -", "May 2024 –", "2024 -"])
def test_a_missing_end_is_recognised_and_flagged(line: str) -> None:
    assert date_only(line) is not None
    assert is_open_ended(line)


@pytest.mark.parametrize("line", ["May 2024 - Present", "Summer 2024", "2017 - 2021"])
def test_a_complete_date_is_not_open_ended(line: str) -> None:
    assert not is_open_ended(line)


# --------------------------------------------------------------------------
# Not dates
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "Software Engineering Intern, Acme Corp",
        "Built async APIs with FastAPI.",
        "Python, PostgreSQL, Docker",
        "",
    ],
)
def test_prose_and_titles_are_not_dates(line: str) -> None:
    assert date_only(line) is None


# --------------------------------------------------------------------------
# Splitting a date off the end of a line
# --------------------------------------------------------------------------


def test_a_trailing_date_splits_from_its_title() -> None:
    assert trailing_date("Staff Engineer, Acme   Mar 2021 - Present") == (
        "Staff Engineer, Acme",
        "Mar 2021 - Present",
    )


def test_a_trailing_shared_year_range_splits() -> None:
    assert trailing_date("Networking Virtual Intern, EduSkills  September - November 2023") == (
        "Networking Virtual Intern, EduSkills",
        "September - November 2023",
    )


def test_a_trailing_bare_year_is_left_alone() -> None:
    """`…State University, 2017` is one entry name.

    Splitting the year off leaves a name ending in a comma, and the year is
    part of how the degree is written rather than a date range beside it.
    """
    assert trailing_date("B.S. Computer Science, State University, 2017") is None


def test_a_line_that_is_only_a_date_does_not_split() -> None:
    """`date_only` handles those. Answering both ways is the original bug:
    the leading whitespace matched after the month, so `Mar 2021 - Present`
    split into the name `Mar`."""
    assert trailing_date("Mar 2021 - Present") is None


# --------------------------------------------------------------------------
# What the ATS scorer does with them
# --------------------------------------------------------------------------

_INTERNSHIPS = """\
Gopi Katkuri
gopi@example.com | linkedin.com/in/gopi

EXPERIENCE

Networking Virtual Intern, EduSkills
September - November 2023
Completed a ten week networking virtual internship.

SKILLS
Python, SQL

EDUCATION
M.S. Computer Science, State University
2024 - 2026
"""


def test_a_resume_dated_only_with_points_is_not_called_undated() -> None:
    """It used to be. The check accepted a range and nothing else, so a
    résumé with a date on every entry was charged 0.15 for having none."""
    codes = {f.code for f in score(parse_text(_INTERNSHIPS)).findings}

    assert "no_dates" not in codes


def test_a_dangling_date_is_reported() -> None:
    dangling = _INTERNSHIPS.replace("September - November 2023", "September 2023 -")
    findings = {f.code: f for f in score(parse_text(dangling)).findings}

    assert "open_ended_date" in findings
    assert "Present" in findings["open_ended_date"].detail, "the finding should say what to do"


def test_a_dangling_date_costs_less_than_no_date_at_all() -> None:
    """It is ambiguous, not absent. The score should say so in proportion."""
    dangling = parse_text(_INTERNSHIPS.replace("September - November 2023", "September 2023 -"))
    undated = parse_text(_INTERNSHIPS.replace("September - November 2023", "Networking team"))

    assert score(dangling).parse > score(undated).parse
