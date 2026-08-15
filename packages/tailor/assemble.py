"""Assemble a résumé document from parsed source plus selected projects.

This is the piece that makes "keep the projects in every résumé" true: the
Projects section is rebuilt from the current project inventory on every
assembly, so a newly pushed repo appears without editing anything by hand.

Two constraints shape the output:

- **Nothing is invented.** Every line comes from the parsed source résumé or
  from a Project record, both of which are source facts. This module does no
  rewriting at all — that arrives in Phase 3, behind the fabrication guard.
- **The layout stays ATS-legible.** No columns, no tables, no text boxes, no
  headers or footers. Those are the standard ways a good-looking résumé
  becomes an unreadable one after a parser gets to it.
"""

from __future__ import annotations

import html

from pydantic import BaseModel, Field

from packages.core.models import Project
from packages.tailor.parse import ParsedResume
from packages.tailor.projects import (
    LinkStyle,
    render_section_html,
)

#: Order sections appear in. Anything parsed but not listed is appended after,
#: in document order, so an unusual section is never silently dropped.
SECTION_ORDER: tuple[str, ...] = (
    "summary",
    "experience",
    "projects",
    "skills",
    "education",
    "certifications",
    "publications",
    "awards",
    "languages",
)

SECTION_TITLES: dict[str, str] = {
    "summary": "Summary",
    "experience": "Experience",
    "projects": "Projects",
    "skills": "Skills",
    "education": "Education",
    "certifications": "Certifications",
    "publications": "Publications",
    "awards": "Awards",
    "languages": "Languages",
    "interests": "Interests",
}

RESUME_CSS = """
@page { size: Letter; margin: 0.55in; }
body { font-family: "DejaVu Sans", Helvetica, Arial, sans-serif;
       font-size: 10pt; line-height: 1.32; color: #111; }
header { margin-bottom: 10pt; }
h1 { font-size: 16pt; margin: 0 0 2pt; letter-spacing: 0.01em; }
.contact { font-size: 9pt; color: #333; }
.contact a { color: #1a4d8f; text-decoration: none; }
h2 { font-size: 11pt; margin: 12pt 0 4pt; text-transform: uppercase;
     letter-spacing: 0.05em; border-bottom: 0.6pt solid #999; padding-bottom: 2pt; }
ul { list-style: none; padding: 0; margin: 0; }
li { margin: 0 0 4pt; }
.project-name { font-weight: 700; }
.project-link { color: #1a4d8f; text-decoration: none; white-space: nowrap; }
"""


class AssemblyOptions(BaseModel):
    """How to build this particular résumé."""

    #: Projects are rebuilt on every assembly, so the section stays current.
    include_projects: bool = True
    link_style: LinkStyle = LinkStyle.ICON_SLUG
    projects_heading: str = "Projects"
    #: Replace a source Projects section rather than printing two of them.
    replace_source_projects: bool = True
    sections: tuple[str, ...] = SECTION_ORDER


def _render_contact(resume: ParsedResume) -> str:
    contact = resume.contact
    bits: list[str] = []
    if contact.email:
        bits.append(
            f'<a href="mailto:{html.escape(contact.email)}">{html.escape(contact.email)}</a>'
        )
    if contact.phone:
        bits.append(html.escape(contact.phone))
    for link in contact.links:
        href = link if link.startswith("http") else f"https://{link}"
        bits.append(f'<a href="{html.escape(href, quote=True)}">{html.escape(link)}</a>')

    name = html.escape(contact.name) if contact.name else ""
    line = " &middot; ".join(bits)
    return f"<header>\n<h1>{name}</h1>\n<div class='contact'>{line}</div>\n</header>"


def _render_lines(lines: list[str]) -> str:
    items = "\n".join(f"<li>{html.escape(line)}</li>" for line in lines)
    return f"<ul>\n{items}\n</ul>"


def assemble_html(
    resume: ParsedResume,
    projects: list[Project] | None = None,
    options: AssemblyOptions | None = None,
) -> str:
    """Build the résumé body as HTML.

    The source résumé's own text is reproduced verbatim; only the Projects
    section is generated, and only from Project records.
    """
    opts = options or AssemblyOptions()
    chosen = projects or []

    parts = [_render_contact(resume)]

    rendered_projects = ""
    if opts.include_projects and chosen:
        rendered_projects = render_section_html(
            chosen, style=opts.link_style, heading=opts.projects_heading
        )

    emitted: set[str] = set()

    for name in opts.sections:
        if name == "projects":
            if rendered_projects:
                parts.append(rendered_projects)
                emitted.add("projects")
            elif resume.section("projects"):
                parts.append(
                    f"<section><h2>{html.escape(SECTION_TITLES['projects'])}</h2>"
                    f"{_render_lines(resume.section('projects'))}</section>"
                )
                emitted.add("projects")
            continue

        lines = resume.section(name)
        if not lines:
            continue
        title = SECTION_TITLES.get(name, name.title())
        parts.append(f"<section><h2>{html.escape(title)}</h2>{_render_lines(lines)}</section>")
        emitted.add(name)

    # A section the parser found but the layout does not know about is kept,
    # not dropped — losing content from someone's résumé is the worse failure.
    for name, lines in resume.sections.items():
        if name in emitted or not lines:
            continue
        if name == "projects" and opts.replace_source_projects and rendered_projects:
            continue
        title = SECTION_TITLES.get(name, name.title())
        parts.append(f"<section><h2>{html.escape(title)}</h2>{_render_lines(lines)}</section>")

    return "\n".join(parts)


def assemble_pdf(
    resume: ParsedResume,
    projects: list[Project] | None = None,
    options: AssemblyOptions | None = None,
) -> bytes:
    """Render the assembled résumé to PDF bytes."""
    from weasyprint import CSS, HTML

    body = assemble_html(resume, projects, options)
    document = HTML(string=f"<body>{body}</body>")
    return bytes(document.write_pdf(stylesheets=[CSS(string=RESUME_CSS)]))


class AssemblyReport(BaseModel):
    """What went into the assembled document, for the review screen."""

    sections: list[str] = Field(default_factory=list)
    project_names: list[str] = Field(default_factory=list)
    source_line_count: int = 0


def describe(
    resume: ParsedResume,
    projects: list[Project] | None = None,
    options: AssemblyOptions | None = None,
) -> AssemblyReport:
    """Summarize an assembly without rendering it."""
    opts = options or AssemblyOptions()
    chosen = projects or []
    included = [name for name in opts.sections if resume.section(name)]
    if opts.include_projects and chosen and "projects" not in included:
        included.append("projects")
    return AssemblyReport(
        sections=included,
        project_names=[p.name for p in chosen],
        source_line_count=len(resume.raw_lines),
    )
