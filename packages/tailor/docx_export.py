"""A résumé as a single-column .docx, for application sites that ask for Word.

The same rules `assemble.py` keeps for the PDF, because an ATS reads both:
one column, no tables, no text boxes, no frames, and section headings the
parser recognizes (`parse.SECTION_PATTERNS`), so `parse_resume` reads the file
back into the same sections. A bullet uses Word's own list style rather than
a typed glyph, so its text comes back without one.

GitHub projects are set the way the PDF's generated section sets them: name
and description as GitHub reports them, then language and topics, then the
address, which a private repository never prints (`projects.ProjectEntry`).
"""

from __future__ import annotations

import io
from collections.abc import Iterable, Sequence

from docx import Document
from docx.document import Document as WordDocument
from docx.shared import Inches, Pt, RGBColor
from docx.text.paragraph import Paragraph

from packages.core.models import Project
from packages.tailor.assemble import SECTION_ORDER, SECTION_TITLES
from packages.tailor.bullets import LineKind, classify
from packages.tailor.parse import ParsedResume
from packages.tailor.projects import ProjectEntry

_ENTRY_SECTIONS: frozenset[str] = frozenset(
    {"experience", "projects", "education", "certifications", "publications", "awards"}
)
_MUTED = RGBColor(0x4A, 0x55, 0x68)
_LIST_STYLE = "List Bullet"


def _ordered(
    sections: dict[str, list[str]], with_projects: bool
) -> Iterable[tuple[str, list[str]]]:
    seen: set[str] = set()
    for name in SECTION_ORDER:
        lines = sections.get(name) or []
        if lines or (name == "projects" and with_projects):
            seen.add(name)
            yield name, lines
    for name, lines in sections.items():
        if name not in seen and lines:
            yield name, lines


def _spaced(paragraph: Paragraph, before: float, after: float) -> Paragraph:
    paragraph.paragraph_format.space_before = Pt(before)
    paragraph.paragraph_format.space_after = Pt(after)
    return paragraph


def _heading(document: WordDocument, title: str) -> None:
    paragraph = _spaced(document.add_paragraph(), 8, 3)
    paragraph.paragraph_format.keep_with_next = True
    run = paragraph.add_run(title.upper())
    run.bold = True
    run.font.size = Pt(10.5)


def _entry_line(document: WordDocument, line: str) -> None:
    kind = classify(line)
    if kind is LineKind.BULLET:
        _spaced(document.add_paragraph(line, style=_LIST_STYLE), 0, 1.5)
        return
    paragraph = _spaced(document.add_paragraph(), 3 if kind is LineKind.ENTRY else 0, 1.5)
    paragraph.paragraph_format.keep_with_next = True
    run = paragraph.add_run(line)
    if kind is LineKind.ENTRY:
        run.bold = True
    else:
        run.italic = True
        run.font.color.rgb = _MUTED


def _project(document: WordDocument, project: Project) -> None:
    entry = ProjectEntry.from_project(project)
    paragraph = _spaced(document.add_paragraph(), 3, 1.5)
    paragraph.paragraph_format.keep_with_next = True
    paragraph.add_run(entry.name).bold = True
    if entry.description:
        paragraph.add_run(f" — {entry.description}")
    details = [", ".join(entry.stack)] if entry.stack else []
    if not entry.is_private:
        details.append(entry.slug)
    if details:
        run = _spaced(document.add_paragraph(), 0, 1.5).add_run(" · ".join(details))
        run.italic = True
        run.font.color.rgb = _MUTED


def render_docx(resume: ParsedResume, projects: Sequence[Project] = ()) -> bytes:
    """`resume`, and the generated `projects` ahead of its own, as .docx bytes."""
    document = Document()
    for section in document.sections:
        section.top_margin = section.bottom_margin = Inches(0.55)
        section.left_margin = section.right_margin = Inches(0.65)
    normal = document.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(10)

    contact = resume.contact
    if contact.name:
        run = _spaced(document.add_paragraph(), 0, 2).add_run(contact.name)
        run.bold = True
        run.font.size = Pt(16)
    parts = [p.strip() for p in (contact.email, contact.phone, *contact.links) if p and p.strip()]
    if parts:
        run = _spaced(document.add_paragraph(), 0, 8).add_run(" | ".join(parts))
        run.font.color.rgb = _MUTED

    for name, lines in _ordered(resume.sections, bool(projects)):
        kept = [line.strip() for line in lines if line.strip()]
        _heading(document, SECTION_TITLES.get(name, name.replace("_", " ").title()))
        if name == "projects":
            for project in projects:
                _project(document, project)
        for line in kept:
            if name in _ENTRY_SECTIONS:
                _entry_line(document, line)
            else:
                _spaced(document.add_paragraph(line), 1, 2)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()
