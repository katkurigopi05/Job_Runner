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
