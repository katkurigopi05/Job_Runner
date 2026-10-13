"""Where a rendered résumé's text falls on its pages.

Gate 3 renders a PDF, reads its text back and compares. It never asked where
on a page the text sits, and a document can pass it and still look wrong: on
2026-10-12 the owner's storage held a tailored résumé (`v4/tailored.pdf`) of
three pages with two lines on the third. Tailoring had made one page of
bullets a little longer, and the last of them went over.

The idea is `tools/verify_layout.py` in MadsLorentzen/ai-job-search (MIT),
which measures a compiled LaTeX CV because "checked by looking at the rendered
page … is exactly how they get missed". What is taken is the list of things
worth measuring. The measurements are this project's own, for a page WeasyPrint
draws:

- **Read with pypdf**, which `parse.py` already uses, where that tool needs
  Poppler's bounding boxes. Every run of text arrives with the position it is
  drawn at; runs on one baseline are one printed line.
- **A heading is a heading because the document says so.** That tool infers
  one from a taller line. Here a section heading is set smaller than the body,
  and capitals prove nothing (`AWS, GCP, SQL` is a line of a résumé), so the
  caller passes the headings the document has.
- **Thin is counted in lines**, and only for a page the text spilled onto. A
  one-page résumé with ten lines is short, which is a different thing.

It reports and changes nothing. What to cut so two lines come back is the
owner's decision, and the review screen is where they can make it.
"""

from __future__ import annotations

import io
from collections.abc import Iterable
from dataclasses import dataclass

import structlog
from pydantic import BaseModel, Field

from packages.tailor.assemble import SECTION_TITLES

log = structlog.get_logger(__name__)

#: A last page with this many lines or fewer is a spill, not a page. Six is
#: one short entry: a name, a line under it and a few bullets.
THIN_LAST_PAGE_LINES = 6

#: A page that is not the last and stops with more than this share of its
#: height unused. The template's own bottom margin is 5%, and a page broken
#: in the ordinary way ends within a few lines of it.
EARLY_END_SHARE = 0.25

#: Runs of text whose baselines are this close are one printed line. Lines of
#: 10pt text are 13pt apart.
_SAME_LINE_PT = 2.0

#: The Projects section is printed under one of these (`assemble_html`).
_PROJECT_HEADINGS = ("Projects", "Selected Project Experience")


@dataclass(frozen=True)
class PageFill:
    """One page: its lines top to bottom, and how much of it is left under them."""

    lines: tuple[str, ...]
    #: Share of the page's height below the last line's baseline. 1.0 when empty.
    empty_below: float


class LayoutFinding(BaseModel):
    """One thing about the pages the owner would want to see before sending."""

    code: str
    page: int
    detail: str


class LayoutReport(BaseModel):
    """How a document sits on its pages. Numbers and findings, never its text."""

    pages: int
    #: Printed lines on each page, in order.
    lines: list[int] = Field(default_factory=list)
    findings: list[LayoutFinding] = Field(default_factory=list)


def section_headings(sections: Iterable[str]) -> frozenset[str]:
    """The headings a résumé with these sections is printed under, casefolded."""
    printed: set[str] = set()
    for name in sections:
        if name == "projects":
            printed.update(_PROJECT_HEADINGS)
        printed.add(SECTION_TITLES.get(name, name.title()))
    return frozenset(heading.casefold() for heading in printed)


def measure_pages(pdf: bytes) -> list[PageFill]:
    """Each page's printed lines. Raises if `pdf` cannot be read as one."""
    from pypdf import PdfReader

    pages: list[PageFill] = []
    for page in PdfReader(io.BytesIO(pdf)).pages:
        runs: list[tuple[float, float, str]] = []

        def visit(text, cm, tm, _font, _size, runs=runs) -> None:  # type: ignore[no-untyped-def]
            if text.strip():
                # Where the run is drawn: its text matrix, through the page's own.
                x = cm[0] * tm[4] + cm[2] * tm[5] + cm[4]
                y = cm[1] * tm[4] + cm[3] * tm[5] + cm[5]
                runs.append((y, x, text))

        page.extract_text(visitor_text=visit)
        height = float(page.mediabox.height)
        # Top of the page first: a PDF counts up from the bottom edge.
        runs.sort(key=lambda run: (-run[0], run[1]))
        grouped: list[list[tuple[float, float, str]]] = []
        for run in runs:
            if grouped and abs(grouped[-1][0][0] - run[0]) <= _SAME_LINE_PT:
                grouped[-1].append(run)
            else:
                grouped.append([run])
        lines = tuple(
            " ".join(" ".join(text for _, _, text in sorted(line, key=lambda r: r[1])).split())
            for line in grouped
        )
        lowest = min((run[0] for run in runs), default=height)
        pages.append(PageFill(lines=lines, empty_below=max(0.0, min(1.0, lowest / height))))
    return pages


def _findings(pages: list[PageFill], headings: frozenset[str]) -> list[LayoutFinding]:
    found: list[LayoutFinding] = []
    last = len(pages)
    for number, page in enumerate(pages, start=1):
        if not page.lines:
            found.append(
                LayoutFinding(code="empty_page", page=number, detail=f"Page {number} has no text.")
            )
            continue
        if number == last:
            count = len(page.lines)
            if last > 1 and count <= THIN_LAST_PAGE_LINES:
                found.append(
                    LayoutFinding(
                        code="thin_last_page",
                        page=number,
                        detail=(
                            f"Page {number} holds only {count} line{'' if count == 1 else 's'}. "
                            f"Cutting that much earlier would end the document on "
                            f"page {number - 1}."
                        ),
                    )
                )
            continue
        if page.lines[-1].casefold() in headings:
            found.append(
                LayoutFinding(
                    code="stranded_heading",
                    page=number,
                    detail=(
                        f"Page {number} ends on the heading “{page.lines[-1].title()}”, "
                        f"with what it introduces on page {number + 1}."
                    ),
                )
            )
        elif page.empty_below > EARLY_END_SHARE:
            found.append(
                LayoutFinding(
                    code="page_ends_early",
                    page=number,
                    detail=(
                        f"Page {number} stops with {page.empty_below:.0%} of it empty, "
                        f"and page {number + 1} follows."
                    ),
                )
            )
    return found


def measure(pdf: bytes, *, headings: Iterable[str] = ()) -> LayoutReport | None:
    """How `pdf` sits on its pages, or None when it cannot be read as a PDF.

    None is "not measured", for a DOCX upload or a damaged file. It is never a
    clean report: a screen that cannot tell the two apart would show a
    document nobody looked at as one with nothing wrong.
    """
    try:
        pages = measure_pages(pdf)
    except Exception as exc:  # noqa: BLE001 - any unreadable file is "not measured"
        log.info("layout_not_measured", error=type(exc).__name__)
        return None
    if not pages:
        return None
    return LayoutReport(
        pages=len(pages),
        lines=[len(page.lines) for page in pages],
        findings=_findings(pages, frozenset(heading.casefold() for heading in headings)),
    )


__all__ = [
    "EARLY_END_SHARE",
    "THIN_LAST_PAGE_LINES",
    "LayoutFinding",
    "LayoutReport",
    "PageFill",
    "measure",
    "measure_pages",
    "section_headings",
]
