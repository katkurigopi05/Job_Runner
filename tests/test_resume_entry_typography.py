"""How names, stacks, labels and locations are set on the rendered résumé.

Found on the owner's résumé on 2026-10-24: Skills read `Languages Python, ...`
with nothing between category and list, every project title ended in a
bracketed `[GitHub]` that linked nowhere, a two-item stack and the coursework
line were bold like names, a school ran into its city, and three
certifications were bold names while two were bullets.

A first fix changed `bullets.classify`, which also decides what the model is
asked to rewrite. It made a job title a bullet and a bullet a line nobody
rewrites. These fixes change typography only, and the first block below holds
`classify` where it was.
"""

from __future__ import annotations

import pytest

from packages.tailor.assemble import assemble_html, assemble_pdf, trailing_location
from packages.tailor.bullets import LineKind, classify
from packages.tailor.parse import extract_text, parse_text

RESUME = """\
Jane Doe
jane@example.com | (555) 123-4567 | github.com/janedoe

PROJECTS
Palette Tool — Color Auditing CLI   [GitHub]
Python, GitHub Actions
Built a palette auditing CLI with a full pytest suite.

EXPERIENCE
Barista, Starbucks
Served customers during peak hours and trained new staff.

EDUCATION
California State University East Bay Hayward, CA
Master of Science in Business Analytics Jan 2025 – Dec 2026
Courses: Data Structures & Algorithms, Computer Networks, Operating Systems

SKILLS
Languages  Python, Rust, SQL
Tools: Git, GitHub Actions, Docker

CERTIFICATIONS
Machine Learning with Python — Inmovidu Tech
• 15 Coursera Certifications — including AWS and Project Management
"""


@pytest.fixture(scope="module")
def html() -> str:
    return assemble_html(parse_text(RESUME))


# --------------------------------------------------------------------------
# What the model is asked to rewrite does not move
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "Teaching Assistant, CSUEB",
        "Barista, Starbucks",
        "Research Assistant, Prof. Lee's NLP Lab",
    ],
)
def test_a_job_title_is_still_a_name_and_never_sent_to_the_model(line: str) -> None:
    assert classify(line) is LineKind.ENTRY


def test_a_labelled_bullet_with_one_comma_is_still_rewritten() -> None:
    assert classify("Result: cut latency 40%, saved $2M") is LineKind.BULLET


# --------------------------------------------------------------------------
# Skills
# --------------------------------------------------------------------------


def test_a_skills_category_is_bold_and_followed_by_a_colon(html: str) -> None:
    assert "<strong>Languages:</strong> Python, Rust, SQL" in html


def test_a_category_already_written_with_a_colon_gets_one_colon(html: str) -> None:
    assert "<strong>Tools:</strong> Git, GitHub Actions, Docker" in html
    assert "Tools::" not in html


def test_the_colon_survives_into_the_text_an_ats_reads() -> None:
    text = extract_text(assemble_pdf(parse_text(RESUME)), "resume.pdf")
    assert "Languages: Python, Rust, SQL" in text


# --------------------------------------------------------------------------
# Titles and their supporting lines
# --------------------------------------------------------------------------


def test_a_link_annotation_is_not_printed_on_the_title(html: str) -> None:
    assert "[GitHub]" not in html
    assert ">Palette Tool — Color Auditing CLI</span>" in html


def test_the_annotation_still_tells_classify_the_line_is_a_title() -> None:
    assert classify("Palette Tool — Color Auditing CLI   [GitHub]") is LineKind.ENTRY


def test_a_two_item_stack_of_listed_skills_is_set_as_a_stack(html: str) -> None:
    assert "<p class='entry-meta'>Python, GitHub Actions</p>" in html


def test_a_role_and_employer_of_the_same_shape_is_still_a_name(html: str) -> None:
    assert "<span class='entry-name'>Barista, Starbucks</span>" in html


def test_a_labelled_list_under_a_degree_is_set_as_supporting(html: str) -> None:
    assert (
        "<p class='entry-meta'>Courses: Data Structures &amp; Algorithms, "
        "Computer Networks, Operating Systems</p>"
    ) in html


# --------------------------------------------------------------------------
# Education locations
# --------------------------------------------------------------------------


def test_a_school_is_set_apart_from_its_city(html: str) -> None:
    assert (
        "<span class='entry-name'>California State University East Bay</span>"
        "<span class='entry-location'>Hayward, CA</span>"
    ) in html


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("University of Utah Salt Lake City, UT", ("University of Utah", "Salt Lake City, UT")),
        ("IIT Delhi New Delhi, India", ("IIT Delhi", "New Delhi, India")),
        ("SR University Warangal, India", ("SR University", "Warangal, India")),
    ],
)
def test_a_listed_city_of_several_words_is_kept_whole(line: str, expected: tuple[str, str]) -> None:
    assert trailing_location(line) == expected


def test_a_line_that_names_no_school_is_not_split() -> None:
    assert trailing_location("Data Engineer, Stripe, NY") is None


def test_an_employment_line_is_never_split_for_a_location() -> None:
    resume = RESUME.replace("Barista, Starbucks", "Acme University Press Oakland, CA")
    html = assemble_html(parse_text(resume))
    assert "<span class='entry-name'>Acme University Press Oakland, CA</span>" in html


# --------------------------------------------------------------------------
# Certifications
# --------------------------------------------------------------------------


def test_undated_certifications_are_one_list_with_no_doubled_glyph(html: str) -> None:
    assert "<li>Machine Learning with Python — Inmovidu Tech</li>" in html
    assert "<li>15 Coursera Certifications — including AWS and Project Management</li>" in html
    assert "<li>• " not in html


def test_dated_certifications_keep_the_entry_layout() -> None:
    resume = RESUME.replace(
        "Machine Learning with Python — Inmovidu Tech",
        "AWS Certified Cloud Practitioner Jan 2024 – Jan 2027",
    )
    html = assemble_html(parse_text(resume))
    assert "<span class='entry-name'>AWS Certified Cloud Practitioner</span>" in html
