"""ATS-safe single-column DOCX exporter for parsed résumés.

Matches the structure rules of `packages/tailor/assemble.py`:
- Single column, zero tables, zero text boxes, zero floating frames.
- Inline entry dates so an ATS parser never splits a role or degree from its dates.
- Standard section headings matching `SECTION_TITLES` so `parse_resume` round-trips cleanly.
"""

from __future__ import annotations

import io
from collections.abc import Iterable

from docx import Document
from docx.shared import Inches, Pt, RGBColor

from packages.tailor.assemble import SECTION_ORDER, SECTION_TITLES
from packages.tailor.bullets import LineKind, classify
from packages.tailor.parse import ParsedResume

_ENTRY_SECTIONS: frozenset[str] = frozenset(
    {"experience", "projects", "education", "certifications", "publications", "awards"}
)
_PLAIN_SECTIONS: frozenset[str] = frozenset({"skills", "languages", "interests"})


def _ordered_section_items(sections: dict[str, list[str]]) -> Iterable[tuple[str, list[str]]]:
    seen: set[str] = set()
    for name in SECTION_ORDER:
        lines = sections.get(name)
        if lines:
            seen.add(name)
            yield name, lines
    for name, lines in sections.items():
        if name not in seen and lines:
            yield name, lines


def render_docx(resume: ParsedResume) -> bytes:
    """Render `resume` to an ATS-safe single-column DOCX byte payload."""
    doc = Document()
    for section in doc.sections:
        section.top_margin = Inches(0.55)
        section.bottom_margin = Inches(0.55)
        section.left_margin = Inches(0.65)
        section.right_margin = Inches(0.65)

    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(10)

    contact = resume.contact
    if contact.name:
        heading = doc.add_paragraph()
        heading.paragraph_format.space_before = Pt(0)
        heading.paragraph_format.space_after = Pt(2)
        run = heading.add_run(contact.name)
        run.bold = True
        run.font.size = Pt(16)

    contact_parts = [p for p in (contact.email, contact.phone, *contact.links) if p and p.strip()]
    if contact_parts:
        meta = doc.add_paragraph(" | ".join(contact_parts))
        meta.paragraph_format.space_before = Pt(0)
        meta.paragraph_format.space_after = Pt(8)
        for run in meta.runs:
            run.font.size = Pt(9.5)
            run.font.color.rgb = RGBColor(0x4A, 0x55, 0x68)

    for key, lines in _ordered_section_items(resume.sections):
        cleaned = [line.strip() for line in lines if line.strip()]
        if not cleaned:
            continue

        title_text = SECTION_TITLES.get(key, key.replace("_", " ").title())
        sec_heading = doc.add_paragraph()
        sec_heading.paragraph_format.space_before = Pt(8)
        sec_heading.paragraph_format.space_after = Pt(3)
        sec_heading.paragraph_format.keep_with_next = True
        title_run = sec_heading.add_run(title_text.upper())
        title_run.bold = True
        title_run.font.size = Pt(10.5)

        if key in _ENTRY_SECTIONS:
            for line in cleaned:
                kind = classify(line)
                para = doc.add_paragraph()
                para.paragraph_format.space_before = Pt(3 if kind is LineKind.ENTRY else 0)
                para.paragraph_format.space_after = Pt(1.5)
                if kind is LineKind.ENTRY:
                    para.paragraph_format.keep_with_next = True
                    run = para.add_run(line)
                    run.bold = True
                    run.font.size = Pt(10)
                elif kind is LineKind.META:
                    para.paragraph_format.keep_with_next = True
                    run = para.add_run(line)
                    run.italic = True
                    run.font.size = Pt(9.5)
                    run.font.color.rgb = RGBColor(0x4A, 0x55, 0x68)
                else:
                    para.paragraph_format.left_indent = Inches(0.18)
                    para.add_run(f"• {line}").font.size = Pt(9.8)
        elif key in _PLAIN_SECTIONS:
            for line in cleaned:
                para = doc.add_paragraph(line)
                para.paragraph_format.space_before = Pt(1)
                para.paragraph_format.space_after = Pt(2)
        else:
            for line in cleaned:
                para = doc.add_paragraph(line)
                para.paragraph_format.space_before = Pt(1)
                para.paragraph_format.space_after = Pt(2.5)

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
