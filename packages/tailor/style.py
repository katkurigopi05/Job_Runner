"""A résumé's face and sizes, and fitting it to its pages.

Two things a tailored résumé needed that the template could not do.

**Fitting.** A tailored résumé in the owner's storage (`v4/tailored.pdf`,
2026-10-12) ran to three pages with two lines on the third. `layout.py`
reports that and changes nothing, so the review screen was the only place to
fix it, by cutting. `fit_to_pages` tightens the type first, one step at a
time, and stops at the first step that pulls the spill back. It changes
spacing and size and never a word, so nothing here is a §2.1 question.

The limits are `ResumeStyle`'s own bounds and `FIT_FLOOR`: body type no
smaller than 9.5pt and margins no narrower than half an inch, the floor most
résumé guidance puts on readable print. A page limit the floor cannot reach
is reported and left alone. Squeezing to the floor and still running over
would cost the reader and buy nothing.

**The face.** Every size follows the body size, so a fitted résumé keeps its
proportions. A chosen face is checked against the fonts the rendered PDF
embeds (`uses_face`), because a stylesheet's request is not what prints: the
template asked for "DejaVu Sans" for months and every PDF came out in
Helvetica.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from functools import cache
from string import Template
from typing import NamedTuple

from pydantic import BaseModel, ConfigDict, Field

from packages.core.enums import ResumeFont


class FontFace(NamedTuple):
    #: How the owner and the review notes name it.
    label: str
    #: The `font-family` value.
    css: str
    #: What the face's PostScript names start with in an embedded PDF font
    #: (`Georgia-Bold`, `TimesNewRomanPSMT`). Empty for the default, which is
    #: a list of fallbacks and is not checked.
    postscript: str


FONT_FACES: dict[ResumeFont, FontFace] = {
    ResumeFont.DEFAULT: FontFace(
        "the default face", '"DejaVu Sans", Helvetica, Arial, sans-serif', ""
    ),
    ResumeFont.HELVETICA: FontFace("Helvetica", "Helvetica, sans-serif", "Helvetica"),
    ResumeFont.ARIAL: FontFace("Arial", "Arial, sans-serif", "Arial"),
    ResumeFont.GEORGIA: FontFace("Georgia", "Georgia, serif", "Georgia"),
    ResumeFont.TIMES: FontFace("Times New Roman", '"Times New Roman", serif', "TimesNewRoman"),
}


class ResumeStyle(BaseModel):
    """The sizes a résumé is set at. The defaults are the template as it was."""

    model_config = ConfigDict(frozen=True)

    font: ResumeFont = ResumeFont.DEFAULT
    body_pt: float = Field(default=10.0, ge=9.5, le=12.0)
    line_height: float = Field(default=1.34, ge=1.2, le=1.6)
    margin_in: float = Field(default=0.55, ge=0.5, le=1.0)
    #: Space above a section heading.
    section_gap_pt: float = Field(default=13.0, ge=8.0, le=18.0)
    #: Space under each bullet. Fixed while the rest tightened, it held a
    #: bullet-heavy page to 14% more lines at the floor, short of a 7-line spill.
    item_gap_pt: float = Field(default=3.0, ge=1.0, le=6.0)


#: The tightest fitting goes. Every value is inside `ResumeStyle`'s bounds.
FIT_FLOOR: dict[str, float] = {
    "body_pt": 9.5,
    "line_height": 1.2,
    "margin_in": 0.5,
    "section_gap_pt": 9.0,
    "item_gap_pt": 1.5,
}

#: Tried in order until one pulls the spill back. Spacing goes before size:
#: a reader notices smaller type sooner than a tighter line.
FIT_STEPS: tuple[Mapping[str, float], ...] = (
    {"line_height": 1.28, "margin_in": 0.5, "section_gap_pt": 11.0, "item_gap_pt": 2.5},
    {
        "body_pt": 9.75,
        "line_height": 1.24,
        "margin_in": 0.5,
        "section_gap_pt": 10.0,
        "item_gap_pt": 2.0,
    },
    FIT_FLOOR,
)

_RESUME_TEMPLATE = Template(
    """
@page { size: Letter; margin: ${margin}in; }
body { font-family: ${family};
       font-size: ${body}pt; line-height: ${line_height}; color: #111; }
header { margin-bottom: 12pt; }
h1 { font-size: ${name}pt; font-weight: 700; margin: 0 0 6pt;
     letter-spacing: 0.01em; line-height: 1.2; }
.contact { font-size: ${small}pt; color: #333; line-height: 1.5; }
.contact a { color: #1a4d8f; text-decoration: none; }
h2 { font-size: ${heading}pt; font-weight: 700; margin: ${gap}pt 0 5pt;
     text-transform: uppercase; letter-spacing: 0.08em; color: #222;
     border-bottom: 0.7pt solid #888; padding-bottom: 2.5pt; }
p { margin: 0 0 4pt; }
.entry { margin: 6pt 0 2pt; break-after: avoid; }
.entry:first-child { margin-top: 0; }
.entry-meta + ul { break-before: avoid; }
li { break-inside: avoid; }
.entry-name { font-weight: 700; }
.entry-date { font-size: ${small}pt; color: #444; font-weight: 400; }
.entry-date::before { content: "  \\00b7  "; color: #999; }
.entry-location { font-size: ${small}pt; color: #444; font-weight: 400; font-style: italic; }
.entry-location::before { content: "  \\00b7  "; color: #999; font-style: normal; }
.entry-meta { font-style: italic; color: #444; font-size: ${small}pt; margin: 0 0 3pt; }
ul { list-style: none; padding: 0; margin: 0 0 2pt; }
li { margin: 0 0 ${item_gap}pt; padding-left: 11pt; text-indent: -11pt; }
li::before { content: "\\2022  "; color: #555; }
.plain { list-style: none; padding: 0; margin: 0; }
.plain li { padding-left: 0; text-indent: 0; }
.plain li::before { content: ""; }
.project-name { font-weight: 700; }
.project-link { color: #1a4d8f; text-decoration: none; white-space: nowrap; }
"""
)

#: A letter is prose, not a résumé: no rules, no small caps, wider leading.
#: Same face as the résumé, so the two documents an employer opens together
#: do not look like they came from different people.
_LETTER_TEMPLATE = Template(
    """
@page { size: Letter; margin: 1in; }
body { font-family: ${family};
       font-size: 10.5pt; line-height: 1.5; color: #111; }
p { margin: 0 0 10pt; }
"""
)


def _number(value: Decimal | float) -> str:
    """`10`, `9.5`, `0.55`: what a person would write in a stylesheet."""
    return format(Decimal(str(value)).normalize(), "f")


def _scaled(body: float, ratio: str) -> str:
    """A size that follows the body size, to the hundredth of a point.

    In decimal, so 0.95 x 9.5 is 9.03 and not the 9.02 binary rounding gives.
    """
    value = (Decimal(str(body)) * Decimal(ratio)).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return _number(value)


def resume_css(style: ResumeStyle) -> str:
    """The résumé stylesheet for `style`.

    Name 1.7x the body, section headings 0.95x, dates and the contact line
    0.9x: the ratios the template's 17, 9.5 and 9pt had to its 10pt body.
    """
    return _RESUME_TEMPLATE.substitute(
        margin=_number(style.margin_in),
        family=FONT_FACES[style.font].css,
        body=_number(style.body_pt),
        line_height=_number(style.line_height),
        name=_scaled(style.body_pt, "1.7"),
        small=_scaled(style.body_pt, "0.9"),
        heading=_scaled(style.body_pt, "0.95"),
        gap=_number(style.section_gap_pt),
        item_gap=_number(style.item_gap_pt),
    )


def letter_css(font: ResumeFont) -> str:
    return _LETTER_TEMPLATE.substitute(family=FONT_FACES[font].css)


def embedded_fonts(pdf: bytes) -> frozenset[str]:
    """The PostScript names of the fonts `pdf` embeds, without subset tags."""
    from pypdf import PdfReader

    names: set[str] = set()
    for page in PdfReader(io.BytesIO(pdf)).pages:
        resources = page.get("/Resources")
        fonts = resources.get_object().get("/Font") if resources is not None else None
        if fonts is None:
            continue
        for font in fonts.get_object().values():
            base = str(font.get_object().get("/BaseFont", "")).lstrip("/")
            # A subset is tagged with six capitals and a plus: `ZCESIW+Helvetica`.
            names.add(base.split("+", 1)[1] if "+" in base[:7] else base)
    return frozenset(names)


def page_count(pdf: bytes) -> int:
    from pypdf import PdfReader

    return len(PdfReader(io.BytesIO(pdf)).pages)


def _letters(name: str) -> str:
    return "".join(character for character in name.casefold() if character.isalnum())


def uses_face(pdf: bytes, font: ResumeFont) -> bool:
    """Whether `pdf` was actually set in `font`, rather than a stand-in.

    Compared on letters alone. WeasyPrint names an embedded font after its
    family rather than its PostScript name, so Times New Roman embeds as
    `Times-New-Roman,-Bold` where a PostScript tool would say
    `TimesNewRomanPS-BoldMT`.
    """
    prefix = _letters(FONT_FACES[font].postscript)
    if not prefix:
        return True
    return any(_letters(name).startswith(prefix) for name in embedded_fonts(pdf))


@cache
def face_available(font: ResumeFont) -> bool:
    """Whether WeasyPrint on this machine sets text in `font`."""
    try:
        from weasyprint import CSS, HTML
    except Exception:  # noqa: BLE001 - no WeasyPrint is no face
        return False
    pdf = HTML(string="<p>Aa <b>Aa</b></p>").write_pdf(
        stylesheets=[CSS(string=f"body {{ font-family: {FONT_FACES[font].css}; }}")]
    )
    return uses_face(bytes(pdf), font)


@dataclass(frozen=True)
class Rendered:
    """A rendered résumé, the style it was set at, and what fitting did."""

    pdf: bytes
    style: ResumeStyle
    pages: int
    #: Pages at the style asked for, before any fitting.
    pages_before: int
    #: What changed and why, for the review screen. None when nothing did.
    note: str | None = None

    def stamp(self) -> dict[str, object]:
        """What a stored résumé records about how it was set."""
        return {**self.style.model_dump(mode="json"), "pages": self.pages, "note": self.note}


def tightened(style: ResumeStyle, step: Mapping[str, float]) -> ResumeStyle:
    """`style` with `step` applied, never loosening a value already tighter."""
    return style.model_copy(
        update={field: min(getattr(style, field), value) for field, value in step.items()}
    )


def _lines_per_page(pdf: bytes) -> list[int] | None:
    from packages.tailor.layout import measure_pages

    try:
        return [len(page.lines) for page in measure_pages(pdf)]
    except Exception:  # noqa: BLE001 - an unreadable render is not fitted
        return None


#: A last page holding at most this share of a full page's lines is a spill
#: worth fitting. Wider than `layout.THIN_LAST_PAGE_LINES`, which is what the
#: review screen reports: the owner's `v4` résumé left 7 lines on page 3, one
#: more than that, and one step of tightening pulled them back.
SPILL_SHARE = 0.25


def _target(lines: list[int], max_pages: int | None) -> int | None:
    """How many pages to fit into, or None when the document is fine as it is."""
    from packages.tailor.layout import THIN_LAST_PAGE_LINES

    if max_pages is not None and len(lines) > max_pages:
        return max_pages
    if len(lines) > 1:
        spill = max(THIN_LAST_PAGE_LINES, int(SPILL_SHARE * max(lines[:-1])))
        if lines[-1] <= spill:
            return len(lines) - 1
    return None


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _why(lines: list[int], target: int, max_pages: int | None) -> str:
    if max_pages is not None and len(lines) > max_pages:
        return f"so it fits in {_plural(max_pages, 'page')}"
    return (
        f"so it ends on page {target} instead of leaving "
        f"{_plural(lines[-1], 'line')} alone on page {len(lines)}"
    )


def fit_to_pages(
    render: Callable[[ResumeStyle], bytes],
    style: ResumeStyle,
    *,
    max_pages: int | None = None,
    first: bytes | None = None,
) -> Rendered:
    """Render at `style`, tightening within `FIT_FLOOR` if the pages call for it.

    `first` is a render already made at `style`, so it is not made twice.
    """
    pdf = first if first is not None else render(style)
    lines = _lines_per_page(pdf)
    if not lines:
        return Rendered(pdf=pdf, style=style, pages=0, pages_before=0)
    target = _target(lines, max_pages)
    if target is None:
        return Rendered(pdf=pdf, style=style, pages=len(lines), pages_before=len(lines))

    tried: set[ResumeStyle] = {style}
    for step in FIT_STEPS:
        candidate = tightened(style, step)
        if candidate in tried:
            continue
        tried.add(candidate)
        fitted = render(candidate)
        fitted_lines = _lines_per_page(fitted)
        if fitted_lines and len(fitted_lines) <= target:
            return Rendered(
                pdf=fitted,
                style=candidate,
                pages=len(fitted_lines),
                pages_before=len(lines),
                note=(
                    f"Set at {_number(candidate.body_pt)}pt type, "
                    f"{_number(candidate.line_height)} line spacing and "
                    f"{_number(candidate.margin_in)}in margins "
                    f"{_why(lines, target, max_pages)}."
                ),
            )

    goal = (
        f"fit in {_plural(max_pages, 'page')}"
        if max_pages is not None and len(lines) > max_pages
        else f"pull {_plural(lines[-1], 'line')} back from page {len(lines)}"
    )
    return Rendered(
        pdf=pdf,
        style=style,
        pages=len(lines),
        pages_before=len(lines),
        note=(
            f"Could not {goal} without going below {_number(FIT_FLOOR['body_pt'])}pt type, "
            f"so it is left at {_plural(len(lines), 'page')}. Cutting is the owner's call."
        ),
    )


__all__ = [
    "FIT_FLOOR",
    "FIT_STEPS",
    "FONT_FACES",
    "FontFace",
    "SPILL_SHARE",
    "Rendered",
    "ResumeFont",
    "ResumeStyle",
    "embedded_fonts",
    "face_available",
    "fit_to_pages",
    "letter_css",
    "page_count",
    "resume_css",
    "tightened",
    "uses_face",
]
