"""Where a rendered résumé's text falls on its pages.

A tailored résumé in the owner's storage (`v4/tailored.pdf`, 2026-10-12) ran
to three pages with two lines on the third. Nothing measured that: Gate 3
reads the PDF's text back and never asks where on a page it sits. The checks
here are the ones `tools/verify_layout.py` in MadsLorentzen/ai-job-search
(MIT) makes for a LaTeX CV, taken for a document WeasyPrint draws.

Pages are built with WeasyPrint directly, so each has the lines the test says
it has. The template's own break rules are tested through `assemble_pdf`.
"""

from __future__ import annotations

import pytest

from packages.tailor.assemble import SECTION_TITLES, assemble_pdf
from packages.tailor.layout import (
    THIN_LAST_PAGE_LINES,
    measure,
    measure_pages,
    section_headings,
)
from packages.tailor.parse import parse_text

CSS = "@page { size: Letter; margin: 0.55in; } p { font-size: 10pt; margin: 0 0 4pt; }"


def pdf_of(*pages: list[str]) -> bytes:
    """A PDF with exactly these lines on each page."""
    from weasyprint import CSS as Stylesheet
    from weasyprint import HTML

    body = "".join(
        "<div style='break-after: page'>" + "".join(f"<p>{line}</p>" for line in lines) + "</div>"
        for lines in pages[:-1]
    ) + "".join(f"<p>{line}</p>" for line in pages[-1])
    return bytes(
        HTML(string=f"<body>{body}</body>").write_pdf(stylesheets=[Stylesheet(string=CSS)])
    )


def lines(count: int, word: str = "line") -> list[str]:
    return [f"{word} {number}" for number in range(count)]


def codes(report) -> list[str]:
    return [finding.code for finding in report.findings]


# --------------------------------------------------------------------------
# What is measured
# --------------------------------------------------------------------------


def test_pages_and_their_lines_are_counted() -> None:
    report = measure(pdf_of(lines(40), lines(12)))

    assert report is not None
    assert report.pages == 2
    assert report.lines == [40, 12]


def test_what_is_not_a_pdf_is_not_measured() -> None:
    assert measure(b"PK\x03\x04 a docx is a zip") is None
    assert measure(b"") is None


def test_fragments_on_one_baseline_are_one_line() -> None:
    """A bold name and its date are two runs of text and one printed line."""
    page = measure_pages(pdf_of(["<b>Acme Corp</b> <i>2021</i> <span>Remote</span>"]))[0]

    assert len(page.lines) == 1
    assert page.lines[-1] == "Acme Corp 2021 Remote"


# --------------------------------------------------------------------------
# A last page with almost nothing on it
# --------------------------------------------------------------------------


def test_two_lines_alone_on_a_last_page_are_reported() -> None:
    report = measure(pdf_of(lines(40), lines(40), lines(2)))

    assert report is not None
    assert codes(report) == ["thin_last_page"]
    finding = report.findings[0]
    assert finding.page == 3
    assert "2 lines" in finding.detail


def test_a_last_page_with_real_content_is_not_thin() -> None:
    report = measure(pdf_of(lines(40), lines(THIN_LAST_PAGE_LINES + 1)))

    assert report is not None
    assert codes(report) == []


def test_a_short_one_page_resume_is_not_a_thin_last_page() -> None:
    """Thin is about spilling over. One page with little on it has not."""
    report = measure(pdf_of(lines(3)))

    assert report is not None
    assert codes(report) == []


# --------------------------------------------------------------------------
# A page that stops short, and a heading left behind
# --------------------------------------------------------------------------


def test_a_page_that_stops_short_with_more_to_come_is_reported() -> None:
    report = measure(pdf_of(lines(20), lines(40)))

    assert report is not None
    assert codes(report) == ["page_ends_early"]
    assert report.findings[0].page == 1


def test_a_page_ending_on_a_section_heading_is_reported() -> None:
    report = measure(
        pdf_of([*lines(44), "EXPERIENCE"], lines(40)), headings=section_headings(["experience"])
    )

    assert report is not None
    assert codes(report) == ["stranded_heading"]
    assert report.findings[0].page == 1
    assert "Experience" in report.findings[0].detail


def test_a_last_line_is_a_heading_only_when_it_is_one_of_the_documents() -> None:
    """Without the document's headings nothing is guessed from capitals:
    `AWS, GCP, SQL` is a line of a résumé too."""
    report = measure(pdf_of([*lines(44), "EXPERIENCE"], lines(40)))

    assert report is not None
    assert codes(report) == []


def test_a_page_with_no_text_is_reported() -> None:
    report = measure(pdf_of(lines(40), [""], lines(40)))

    assert report is not None
    assert "empty_page" in codes(report)


def test_the_headings_of_a_document_are_its_sections_as_printed() -> None:
    headings = section_headings(["experience", "skills", "volunteering"])

    assert SECTION_TITLES["experience"].casefold() in headings
    assert "volunteering" in headings
    # Projects is printed under either name (`assemble_html`).
    assert "selected project experience" in section_headings(["projects"])


# --------------------------------------------------------------------------
# The template does not break a page there
# --------------------------------------------------------------------------


def resume_with(filler: int) -> bytes:
    """A résumé whose second section starts `filler` bullets down the page."""
    bullets = "\n".join(
        f"Shipped part {number} of the ingest path and measured what it cost."
        for number in range(filler)
    )
    text = f"""\
Jane Doe
jane@example.com | (555) 123-4567

EXPERIENCE
Acme Corp Jan 2021 - Present
Python, Postgres
{bullets}

EDUCATION
State University Aug 2016 - May 2020
Bachelor of Science in Computer Science
Graduated with honours after four years of coursework and research.
Thesis on incremental view maintenance in streaming systems.
"""
    return assemble_pdf(parse_text(text))


@pytest.fixture(scope="module")
def page_breaks() -> list[tuple[int, str, str]]:
    """Page one's last line and page two's first, as a heading is moved up
    page one a line at a time.

    Page one holds 41 lines, five of them above the first bullet. With 36
    bullets it is full of them. With 35 the heading would be its last line,
    with 34 the entry under the heading would be, and with fewer the entry's
    own lines reach it.
    """
    breaks = []
    for filler in range(31, 39):
        pages = measure_pages(resume_with(filler))
        if len(pages) > 1:
            breaks.append((filler, pages[0].lines[-1], pages[1].lines[0]))
    return breaks


def test_the_page_break_was_walked_across_the_heading(page_breaks) -> None:
    """Or the two tests below could pass by never putting a break near it:
    the heading opens page two in some documents and sits on page one in
    others."""
    opens_page_two = {first.casefold() == "education" for _, _, first in page_breaks}

    assert len(page_breaks) >= 6
    assert opens_page_two == {True, False}


def test_a_section_heading_is_never_the_last_line_of_a_page(page_breaks) -> None:
    """WeasyPrint's own stylesheet does this, and the template relies on it."""
    stranded = [filler for filler, last, _ in page_breaks if last.casefold() == "education"]

    assert stranded == []


def test_an_entry_is_never_parted_from_what_it_introduces(page_breaks) -> None:
    """With 32 and 33 bullets above it, "State University" was the last line
    of page one and its degree the first of page two."""
    orphaned = [filler for filler, last, _ in page_breaks if last.startswith("State University")]

    assert orphaned == []


def test_keeping_an_entry_together_leaves_no_hole_behind_it(page_breaks) -> None:
    """A break may still fall between two bullets, so a page that moves an
    entry on gives up a few lines and not a third of itself."""
    for filler, _, _ in page_breaks:
        report = measure(
            resume_with(filler), headings=section_headings(["experience", "education"])
        )

        assert report is not None
        assert "page_ends_early" not in [finding.code for finding in report.findings], filler


# --------------------------------------------------------------------------
# The review screen is told
# --------------------------------------------------------------------------


@pytest.fixture
def stored(tmp_path):
    from packages.core.storage import LocalStorage, set_storage

    set_storage(LocalStorage(tmp_path / "storage"))
    yield
    set_storage(None)


async def _parsed(client, candidate, name: str, data: bytes) -> dict:
    import io

    created = await client.post(
        "/resumes",
        data={"candidate_id": candidate["candidate_id"]},
        files={"file": (name, io.BytesIO(data), "application/octet-stream")},
    )
    assert created.status_code == 201, created.text
    return (await client.get(f"/resumes/{created.json()['id']}/parsed")).json()


async def test_the_parsed_resume_carries_how_its_file_sits_on_the_page(
    client, complete_candidate, stored
) -> None:
    """`/resumes/{id}/parsed` is what the review screen reads for the
    document it is about to send, so that is where the finding has to be."""
    parsed = await _parsed(client, complete_candidate, "resume.pdf", resume_with(36))

    assert parsed["layout"]["pages"] == 2
    assert [finding["code"] for finding in parsed["layout"]["findings"]] == ["thin_last_page"]
    assert parsed["layout"]["findings"][0]["page"] == 2


async def test_a_file_that_is_not_a_pdf_is_not_measured(client, complete_candidate, stored) -> None:
    """None, and not an empty report: nobody looked at where its text falls."""
    text = b"Ada Lovelace\nada@example.com\n\nExperience\nStaff Engineer, Analytical Engines Ltd\n"

    parsed = await _parsed(client, complete_candidate, "resume.txt", text)

    assert parsed["layout"] is None
