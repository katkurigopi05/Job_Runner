"""The rendered résumé has hierarchy, and survives being read back.

Both halves matter and they pull against each other. A layout with no
hierarchy is the one this replaced — every line an `<li>` in a `<ul>` with
`list-style: none`, so a project title, its technology line and its bullets
all rendered identically and the document read as one undifferentiated block.
A layout with too much of it stops being parseable, which is worse.

The round trip is the referee. No ATS vendor publishes its parser, so the only
evidence available is rendering a PDF and reading it back with our own.
"""

from __future__ import annotations

import pytest

from packages.tailor.assemble import assemble_html, assemble_pdf
from packages.tailor.ats import score
from packages.tailor.parse import extract_text, parse_text

RESUME = """\
Jane Doe
jane@example.com | (555) 123-4567 | github.com/janedoe

SUMMARY
Backend engineer who builds data pipelines.

EXPERIENCE
Acme Corp Jan 2021 - Present
Python, Postgres, Kubernetes
Built the ingest path and cut end-to-end latency in half.
Wrote the migration tool now used by the whole team.

EDUCATION
State University Aug 2016 - May 2020
Bachelor of Science in Computer Science

SKILLS
Languages  Python, Rust, SQL
"""


@pytest.fixture(scope="module")
def rendered() -> str:
    """The résumé rendered to PDF and read back out as text."""
    return extract_text(assemble_pdf(parse_text(RESUME)), "resume.pdf")


# --------------------------------------------------------------------------
# Hierarchy
# --------------------------------------------------------------------------


def test_an_entry_name_is_distinguished_from_its_bullets() -> None:
    html = assemble_html(parse_text(RESUME))
    assert "entry-name" in html
    assert "<li>" in html


def test_a_technology_line_is_marked_as_supporting_not_as_a_bullet() -> None:
    html = assemble_html(parse_text(RESUME))
    assert "entry-meta'>Python, Postgres, Kubernetes" in html


def test_bullets_get_a_real_glyph(rendered: str) -> None:
    """The old layout had `list-style: none` and no marker of any kind."""
    assert "• Built the ingest path" in rendered


def test_a_skills_row_gets_no_bullet_glyph() -> None:
    """`• Languages  Python, Rust, SQL` is a category, not a claim about work."""
    html = assemble_html(parse_text(RESUME))
    assert "plain" in html


def test_the_summary_is_a_paragraph_not_a_bullet(rendered: str) -> None:
    assert "• Backend engineer" not in rendered
    assert "Backend engineer who builds data pipelines." in rendered


# --------------------------------------------------------------------------
# Survives the round trip
# --------------------------------------------------------------------------


def test_the_name_does_not_fuse_into_the_contact_line(rendered: str) -> None:
    """Our own output had the defect the DOCX reader was just fixed for.

    `pypdf` returned `Gopi Krishna Reddy Katkurigkatkuri@horizon…` — the name
    and the email as one token, because `h1` had 2pt of margin under it.
    """
    assert "Jane Doe" in rendered
    assert "Doejane@example.com" not in rendered


def test_a_date_stays_on_the_line_of_the_entry_it_belongs_to(rendered: str) -> None:
    """Why the date is not floated right, recorded as a test.

    With `float: right` the date was drawn in the right place and extracted in
    the wrong one: `pypdf` returned the degree on one line and `Jan 2025 – Dec
    2026` somewhere else, so nothing connected an entry to its dates.
    """
    line = next(line for line in rendered.splitlines() if "Acme Corp" in line)
    assert "Jan 2021" in line


def test_every_section_survives_rendering(rendered: str) -> None:
    reparsed = parse_text(rendered)
    for name in ("summary", "experience", "education", "skills"):
        assert reparsed.section(name), name


def test_the_rendered_resume_scores_clean_when_read_back(rendered: str) -> None:
    """A résumé we generate must not fail the check we apply to uploaded ones."""
    report = score(parse_text(rendered))
    assert report.findings == []
    assert report.parse == 1.0


def test_contact_details_survive_rendering(rendered: str) -> None:
    contact = parse_text(rendered).contact
    assert contact.email == "jane@example.com"
    assert contact.phone is not None
    assert any("github.com/janedoe" in link for link in contact.links)


# --------------------------------------------------------------------------
# ATS constraints
# --------------------------------------------------------------------------


def test_the_layout_uses_no_tables_or_columns() -> None:
    """The standard ways a good-looking résumé becomes an unreadable one."""
    html = assemble_html(parse_text(RESUME))
    for banned in ("<table", "<td", "column-count", "position: absolute"):
        assert banned not in html


# --------------------------------------------------------------------------
# Dates written on their own line — how most résumés write them
# --------------------------------------------------------------------------

#: Employer on one line, dates on the next. The format the renderer got wrong.
_DATES_BELOW = """\
Dana Whitfield
dana@example.com

EXPERIENCE

Staff Engineer, Analytical Engines Ltd
Mar 2021 - Present
Built async APIs with FastAPI, deployed on Kubernetes.

Senior Software Engineer, Cartwright Data
2017 - 2021
Wrote Python services that processed customer events.
"""


def test_a_date_on_its_own_line_does_not_become_an_entry() -> None:
    """The defect: `Mar 2021 - Present` rendered as a bold **Mar**.

    `_TRAILING_DATE_RE` needs whitespace before the date so it can split
    `Acme   Mar 2021 - Present`. On a line that is *only* a date that leading
    `\\s+` matched the space after the month, so the month became the entry
    name and the rest became its date — a second bold row under the job title,
    reading like a second employer.
    """
    html = assemble_html(parse_text(_DATES_BELOW))

    assert "<span class='entry-name'>Mar</span>" not in html
    assert "<span class='entry-name'>Jun</span>" not in html


def test_a_bare_year_range_is_not_rendered_as_an_employer() -> None:
    """`2017 - 2021` matched nothing and became a bold entry name."""
    html = assemble_html(parse_text(_DATES_BELOW))

    assert "<span class='entry-name'>2017 - 2021</span>" not in html


def test_the_date_attaches_to_the_job_above_it() -> None:
    html = assemble_html(parse_text(_DATES_BELOW))

    assert (
        "<span class='entry-name'>Staff Engineer, Analytical Engines Ltd</span>"
        "<span class='entry-date'>Mar 2021 - Present</span>"
    ) in html


def test_one_job_renders_as_one_entry() -> None:
    """Two employers, two entries — not four."""
    html = assemble_html(parse_text(_DATES_BELOW))

    assert html.count("class='entry'") == 2


def test_the_date_survives_into_the_extracted_pdf_text() -> None:
    """What an ATS actually reads. The employer and its dates on one line.

    The existing float-right note explains why this matters: anything that
    moves the date out of the text flow leaves the parser with an entry on one
    line and a date somewhere else, and nothing joining them.
    """
    text = extract_text(assemble_pdf(parse_text(_DATES_BELOW)), "resume.pdf")

    assert "Staff Engineer, Analytical Engines Ltd · Mar 2021 - Present" in text


def test_a_date_beside_the_title_is_still_split() -> None:
    """The case the trailing pattern was written for, unchanged."""
    beside = _DATES_BELOW.replace(
        "Staff Engineer, Analytical Engines Ltd\nMar 2021 - Present",
        "Staff Engineer, Analytical Engines Ltd   Mar 2021 - Present",
    )
    html = assemble_html(parse_text(beside))

    assert (
        "<span class='entry-name'>Staff Engineer, Analytical Engines Ltd</span>"
        "<span class='entry-date'>Mar 2021 - Present</span>"
    ) in html


def test_a_date_is_never_printed_twice() -> None:
    """Written beside the title *and* below it, the résumé said it twice."""
    both = _DATES_BELOW.replace(
        "Staff Engineer, Analytical Engines Ltd\nMar 2021 - Present",
        "Staff Engineer, Analytical Engines Ltd   Mar 2021 - Present\nMar 2021 - Present",
    )
    html = assemble_html(parse_text(both))

    assert html.count("Mar 2021 - Present") == 1
