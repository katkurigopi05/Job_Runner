"""The résumé's face, its sizes, and fitting it to its pages.

A tailored résumé in the owner's storage (`v4/tailored.pdf`) ran to three
pages with two lines on the third. `layout.py` reports that and changes
nothing; the review screen was the only place it could be fixed, by cutting.
Fitting tightens the type within fixed limits first, and never touches a word.

The face was "DejaVu Sans" first, which this Mac does not have, so every PDF
came out in Helvetica while the stylesheet said otherwise. A chosen face is
checked against the fonts the PDF actually embeds.
"""

from __future__ import annotations

import io
import re
import shutil
import subprocess

import pytest
from pydantic import ValidationError

from packages.core.config import Settings
from packages.tailor import style as style_module
from packages.tailor.assemble import RESUME_CSS, AssemblyOptions, assemble_pdf, render_resume
from packages.tailor.layout import THIN_LAST_PAGE_LINES, measure_pages
from packages.tailor.parse import parse_text
from packages.tailor.publish import LETTER_CSS, assembly_options, render_cover_letter
from packages.tailor.style import (
    FIT_FLOOR,
    SPILL_SHARE,
    FontFace,
    ResumeFont,
    ResumeStyle,
    embedded_fonts,
    face_available,
    letter_css,
    resume_css,
)

#: The template the page-layout tests were measured against. The default
#: style has to reproduce it exactly, or every fixture that counts lines on a
#: page is counting a different document.
#:
#: The two `.entry-location` rules were added on 2026-10-24. They style only a
#: span the education layout emits for a school's location, so a document
#: without one is set exactly as before.
TEMPLATE_BEFORE_STYLES = """
@page { size: Letter; margin: 0.55in; }
body { font-family: "DejaVu Sans", Helvetica, Arial, sans-serif;
       font-size: 10pt; line-height: 1.34; color: #111; }
header { margin-bottom: 12pt; }
h1 { font-size: 17pt; font-weight: 700; margin: 0 0 6pt;
     letter-spacing: 0.01em; line-height: 1.2; }
.contact { font-size: 9pt; color: #333; line-height: 1.5; }
.contact a { color: #1a4d8f; text-decoration: none; }
h2 { font-size: 9.5pt; font-weight: 700; margin: 13pt 0 5pt;
     text-transform: uppercase; letter-spacing: 0.08em; color: #222;
     border-bottom: 0.7pt solid #888; padding-bottom: 2.5pt; }
p { margin: 0 0 4pt; }
.entry { margin: 6pt 0 2pt; break-after: avoid; }
.entry:first-child { margin-top: 0; }
.entry-meta + ul { break-before: avoid; }
li { break-inside: avoid; }
.entry-name { font-weight: 700; }
.entry-date { font-size: 9pt; color: #444; font-weight: 400; }
.entry-date::before { content: "  \\00b7  "; color: #999; }
.entry-location { font-size: 9pt; color: #444; font-weight: 400; font-style: italic; }
.entry-location::before { content: "  \\00b7  "; color: #999; font-style: normal; }
.entry-meta { font-style: italic; color: #444; font-size: 9pt; margin: 0 0 3pt; }
ul { list-style: none; padding: 0; margin: 0 0 2pt; }
li { margin: 0 0 3pt; padding-left: 11pt; text-indent: -11pt; }
li::before { content: "\\2022  "; color: #555; }
.plain { list-style: none; padding: 0; margin: 0; }
.plain li { padding-left: 0; text-indent: 0; }
.plain li::before { content: ""; }
.project-name { font-weight: 700; }
.project-link { color: #1a4d8f; text-decoration: none; white-space: nowrap; }
"""

LETTER_BEFORE_STYLES = """
@page { size: Letter; margin: 1in; }
body { font-family: "DejaVu Sans", Helvetica, Arial, sans-serif;
       font-size: 10.5pt; line-height: 1.5; color: #111; }
p { margin: 0 0 10pt; }
"""


def resume_text(bullets: int) -> str:
    lines = "\n".join(
        f"Shipped part {number} of the ingest path and measured what it cost."
        for number in range(bullets)
    )
    return f"""\
Jane Doe
jane@example.com | (555) 123-4567

EXPERIENCE
Acme Corp Jan 2021 - Present
Python, Postgres
{lines}

EDUCATION
State University Aug 2016 - May 2020
Bachelor of Science in Computer Science
Graduated with honours after four years of coursework and research.
"""


def words(pdf: bytes) -> list[str]:
    from pypdf import PdfReader

    text = " ".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages)
    return re.findall(r"\w+", text)


@pytest.fixture(scope="module")
def spilling_resume():
    """A résumé whose default rendering leaves a few lines alone on page two.

    Found by measuring rather than written down, so it holds on a machine
    whose fonts set a line a little wider or narrower than this one's.
    """
    for bullets in range(30, 60):
        parsed = parse_text(resume_text(bullets))
        pages = measure_pages(assemble_pdf(parsed))
        if len(pages) == 2 and len(pages[-1].lines) <= THIN_LAST_PAGE_LINES:
            return parsed
    pytest.fail("no bullet count left a thin second page")


# --------------------------------------------------------------------------
# The default is the template as it was
# --------------------------------------------------------------------------


def test_the_default_style_is_the_template_the_layout_tests_measured() -> None:
    assert resume_css(ResumeStyle()) == TEMPLATE_BEFORE_STYLES
    assert RESUME_CSS == TEMPLATE_BEFORE_STYLES


def test_the_default_letter_is_the_letter_as_it_was() -> None:
    assert letter_css(ResumeFont.DEFAULT) == LETTER_BEFORE_STYLES
    assert LETTER_CSS == LETTER_BEFORE_STYLES


def test_rendering_without_options_does_not_fit() -> None:
    """`assemble_pdf` alone is the template, unchanged; fitting is asked for."""
    assert AssemblyOptions().fit is False


# --------------------------------------------------------------------------
# The limits
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field, value",
    [("body_pt", 9.0), ("body_pt", 13.0), ("margin_in", 0.4), ("line_height", 1.1)],
)
def test_a_style_outside_the_readable_limits_is_refused(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        ResumeStyle(**{field: value})


def test_the_fit_floor_is_inside_the_limits() -> None:
    ResumeStyle(**FIT_FLOOR)


def test_every_size_follows_the_body_size() -> None:
    css = resume_css(ResumeStyle(body_pt=9.5))

    assert "font-size: 9.5pt" in css  # body
    assert "font-size: 16.15pt" in css  # name, 1.7 x body
    assert "font-size: 9.03pt" in css  # section heading, 0.95 x body
    assert "font-size: 8.55pt" in css  # dates and contact line, 0.9 x body


# --------------------------------------------------------------------------
# Fitting
# --------------------------------------------------------------------------


def test_a_few_lines_alone_on_a_last_page_are_pulled_back(spilling_resume) -> None:
    rendered = render_resume(spilling_resume, None, AssemblyOptions(fit=True))

    assert len(measure_pages(rendered.pdf)) == 1
    assert rendered.style != ResumeStyle()
    assert rendered.pages_before == 2
    assert rendered.note is not None and "page 2" in rendered.note


def _two_page_resume(last_page_fits) -> object:
    for bullets in range(30, 80):
        parsed = parse_text(resume_text(bullets))
        lines = [len(page.lines) for page in measure_pages(assemble_pdf(parsed))]
        if len(lines) == 2 and last_page_fits(lines[-1], lines[0]):
            return parsed
    pytest.fail("no bullet count gave that last page")


def test_a_quarter_page_alone_on_a_last_page_is_pulled_back() -> None:
    """More than the review screen calls thin, and still a spill: the owner's
    `v4` résumé left 7 lines on page 3, and one step pulled them back."""
    parsed = _two_page_resume(lambda last, full: THIN_LAST_PAGE_LINES < last <= SPILL_SHARE * full)

    rendered = render_resume(parsed, None, AssemblyOptions(fit=True))

    assert rendered.pages_before == 2
    assert len(measure_pages(rendered.pdf)) == 1


def test_a_last_page_with_more_than_a_quarter_on_it_is_left_alone() -> None:
    parsed = _two_page_resume(lambda last, full: last > SPILL_SHARE * full + 3)

    rendered = render_resume(parsed, None, AssemblyOptions(fit=True))

    assert rendered.style == ResumeStyle()
    assert rendered.note is None


def test_fitting_changes_no_word(spilling_resume) -> None:
    plain = assemble_pdf(spilling_resume)
    fitted = render_resume(spilling_resume, None, AssemblyOptions(fit=True)).pdf

    assert sorted(words(fitted)) == sorted(words(plain))


def test_fitting_never_goes_below_the_floor(spilling_resume) -> None:
    fitted = render_resume(spilling_resume, None, AssemblyOptions(fit=True)).style

    assert fitted.body_pt >= FIT_FLOOR["body_pt"]
    assert fitted.margin_in >= FIT_FLOOR["margin_in"]
    assert fitted.line_height >= FIT_FLOOR["line_height"]


def test_a_document_that_does_not_spill_is_left_as_it_is() -> None:
    rendered = render_resume(parse_text(resume_text(5)), None, AssemblyOptions(fit=True))

    assert rendered.style == ResumeStyle()
    assert rendered.note is None


def test_a_page_limit_out_of_reach_keeps_the_ordinary_type() -> None:
    """Three pages will not become one by shrinking type a little. Squeezing
    to the floor and still failing would cost readability and buy nothing."""
    parsed = parse_text(resume_text(110))
    plain_pages = len(measure_pages(assemble_pdf(parsed)))

    rendered = render_resume(parsed, None, AssemblyOptions(fit=True, max_pages=1))

    assert plain_pages > 2
    assert rendered.style == ResumeStyle()
    assert len(measure_pages(rendered.pdf)) == plain_pages
    assert rendered.note is not None and "1 page" in rendered.note


def test_a_page_limit_within_reach_is_met(spilling_resume) -> None:
    rendered = render_resume(spilling_resume, None, AssemblyOptions(fit=True, max_pages=1))

    assert len(measure_pages(rendered.pdf)) == 1


def test_a_tighter_style_the_owner_chose_is_not_loosened(spilling_resume) -> None:
    chosen = ResumeStyle(body_pt=9.5, line_height=1.2, margin_in=0.5, section_gap_pt=9.0)

    rendered = render_resume(spilling_resume, None, AssemblyOptions(style=chosen, fit=True))

    assert rendered.style.body_pt <= chosen.body_pt
    assert rendered.style.line_height <= chosen.line_height


# --------------------------------------------------------------------------
# The face
# --------------------------------------------------------------------------


def test_only_faces_this_project_can_name_honestly_are_offered() -> None:
    """Calibri and Garamond are not installed with macOS. fontconfig answers
    a request for either with Verdana or Times New Roman, so offering them
    would print a résumé in a face nobody chose."""
    assert {font.value for font in ResumeFont} == {
        "default",
        "helvetica",
        "arial",
        "georgia",
        "times",
    }


def test_a_chosen_face_is_named_first_in_the_stylesheet() -> None:
    css = resume_css(ResumeStyle(font=ResumeFont.GEORGIA))

    assert "font-family: Georgia, serif;" in css


@pytest.mark.skipif(not face_available(ResumeFont.GEORGIA), reason="Georgia is not installed")
def test_a_chosen_face_is_the_one_the_pdf_embeds() -> None:
    rendered = render_resume(
        parse_text(resume_text(5)),
        None,
        AssemblyOptions(style=ResumeStyle(font=ResumeFont.GEORGIA)),
    )

    assert rendered.style.font is ResumeFont.GEORGIA
    assert any(name.startswith("Georgia") for name in embedded_fonts(rendered.pdf))
    assert rendered.note is None


FAMILIES = {
    ResumeFont.HELVETICA: "Helvetica",
    ResumeFont.ARIAL: "Arial",
    ResumeFont.GEORGIA: "Georgia",
    ResumeFont.TIMES: "Times New Roman",
}


def _fontconfig_has(family: str) -> bool:
    """fontconfig's own answer, independent of how the PDF is read."""
    if shutil.which("fc-match") is None:
        return False
    answer = subprocess.run(
        ["fc-match", "-f", "%{family}", family], capture_output=True, text=True, check=False
    ).stdout
    return family.casefold() in {name.strip().casefold() for name in answer.split(",")}


@pytest.mark.parametrize("font", list(FAMILIES))
def test_a_face_fontconfig_has_is_recognised_in_the_pdf(font: ResumeFont) -> None:
    """Asked of every face, not one. WeasyPrint names an embedded font after
    its family, so Times New Roman arrives as `Times-New-Roman,-Bold` and a
    check written for Georgia passed while Times read as not installed."""
    if not _fontconfig_has(FAMILIES[font]):
        pytest.skip(f"{FAMILIES[font]} is not installed")
    face_available.cache_clear()

    assert face_available(font)


def test_a_face_that_is_not_installed_is_said_and_not_substituted(monkeypatch) -> None:
    """A missing family resolves to whatever fontconfig likes. The document
    goes out in the default face and says so, never in a stranger."""
    monkeypatch.setitem(
        style_module.FONT_FACES,
        ResumeFont.GEORGIA,
        FontFace(label="Nowhere", css='"No Such Face 9731", serif', postscript="NoSuchFace"),
    )

    rendered = render_resume(
        parse_text(resume_text(5)),
        None,
        AssemblyOptions(style=ResumeStyle(font=ResumeFont.GEORGIA)),
    )

    assert rendered.style.font is ResumeFont.DEFAULT
    assert rendered.note is not None and "Nowhere" in rendered.note


def test_the_letter_is_set_in_the_resumes_face() -> None:
    assert "font-family: Georgia, serif;" in letter_css(ResumeFont.GEORGIA)


def test_a_letter_renders_in_the_face_it_is_given() -> None:
    pdf = render_cover_letter("Dear team,\n\nHello.", font=ResumeFont.DEFAULT)

    assert words(pdf)[:2] == ["Dear", "team"]


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_the_face_and_fitting_are_settings() -> None:
    settings = Settings(resume_font="georgia", resume_max_pages=2, _env_file=None)

    assert settings.resume_font is ResumeFont.GEORGIA
    assert settings.resume_fit_pages is True
    assert settings.resume_max_pages == 2


def test_a_face_that_cannot_be_offered_is_refused_as_a_setting() -> None:
    with pytest.raises(ValidationError):
        Settings(resume_font="calibri", _env_file=None)


def test_tailoring_renders_with_the_configured_house_style() -> None:
    settings = Settings(resume_font="georgia", resume_max_pages=1, _env_file=None)

    options = assembly_options(parse_text(resume_text(5)), settings=settings)

    assert options.fit is True
    assert options.max_pages == 1
    assert options.style.font is ResumeFont.GEORGIA


def test_a_projects_resume_keeps_its_projects_and_the_house_style() -> None:
    settings = Settings(resume_font="arial", _env_file=None)
    parsed = parse_text(
        """\
Jane Doe
jane@example.com

PROJECTS
Ledger
Built a double-entry ledger in Rust with property tests.
"""
    )

    options = assembly_options(parsed, settings=settings)

    assert options.include_projects is False
    assert options.replace_source_projects is False
    assert options.style.font is ResumeFont.ARIAL
