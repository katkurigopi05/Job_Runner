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
import re

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

#: The layout. Every rule here is either an ATS constraint or the hierarchy a
#: human needs, and the two pull in opposite directions often enough to be
#: worth stating.
#:
#: **Single column, no tables, no text boxes, no header/footer, no images.**
#: Those are the standard ways a good-looking résumé becomes an unreadable one
#: after a parser gets to it, and none of them is used here.
#:
#: **Hierarchy comes from weight and space, not from position.** An entry name
#: is bold, a technology line is muted italic, a bullet is indented under a
#: real `•`. None of that moves text out of reading order, so extraction still
#: yields the document in the order a person reads it.
#:
#: **The date sits inline after its entry name, and is not floated right.**
#: This was tried the other way and the round trip refused it. `float: right`
#: looks better and moves the date out of the extracted text flow: `pypdf`
#: returned `Master of Science in Business Analytics` on one line and `Jan 2025
#: – Dec 2026` somewhere else entirely, so nothing connects a degree to its
#: dates. Inline, extraction gives them as one line, which is what a parser
#: needs. `tests/test_assemble_layout.py` round-trips a rendered PDF back
#: through the parser to hold that, because it is the only evidence available —
#: no ATS vendor publishes its parser, so our own is the referee.
#:
#: **`h1` has real space under it.** Without it `pypdf` merged the name into
#: the contact line and returned `Gopi Krishna Reddy Katkurigkatkuri@…` — our
#: own output reproducing, on the way out, the same fused-field defect the
#: DOCX reader was just fixed for on the way in.
RESUME_CSS = """
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
.entry { margin: 6pt 0 2pt; }
.entry:first-child { margin-top: 0; }
.entry-name { font-weight: 700; }
.entry-date { font-size: 9pt; color: #444; font-weight: 400; }
.entry-date::before { content: "  \\00b7  "; color: #999; }
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


class AssemblyOptions(BaseModel):
    """How to build this particular résumé."""

    #: Projects are rebuilt on every assembly, so the section stays current.
    include_projects: bool = True
    link_style: LinkStyle = LinkStyle.ICON_SLUG
    projects_heading: str = "Projects"
    #: Replace a source Projects section rather than printing two of them.
    #:
    #: Right for an employment résumé, where Projects is supporting material and
    #: the GitHub set should follow the posting. Wrong when the source Projects
    #: section is the résumé's substance — a student or new graduate has their
    #: whole record there, and replacing it with repository names and their raw
    #: GitHub descriptions deletes the document. `publish_tailored` turns this
    #: off when Projects is the section that was tailored.
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


#: Sections built out of entries — a name, optional supporting line, bullets.
#: Everything else is a flat list of lines and is rendered as one.
_ENTRY_SECTIONS: frozenset[str] = frozenset({"experience", "projects", "education"})

#: Sections whose lines are labelled rows rather than bullets. A `•` in front
#: of `Languages  Python, TypeScript, Rust` is noise: it is not a claim about
#: work, it is a category and its members.
_PLAIN_SECTIONS: frozenset[str] = frozenset({"skills", "languages", "interests"})

#: A trailing date range on an entry line, so it can be set to the right.
#:
#: Anchored at the end because that is where a résumé puts it, and a date in
#: the middle of a title is part of the title. A full *range* is required —
#: two endpoints and a dash — which is what makes a single leading space safe
#: to split on: `Product Launch 2024` is a name and keeps its year, while
#: `Master of Science in Business Analytics Jan 2025 – Dec 2026` is a degree
#: and a date. Requiring two spaces instead missed every line the DOCX reader
#: had repaired, since that inserts one.
#: A date range: `Mar 2021 - Present`, `2017 - 2021`, `Jun 2017 – Feb 2021`.
_DATE_BODY = (
    r"(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\w*\.?\s*)?"
    r"(?:19|20)\d{2}\s*[-–—]\s*"
    r"(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\w*\.?\s*)?"
    r"(?:(?:19|20)\d{2}|present|current)\.?"
)

#: A date range *following* an entry name on the same line — `Staff Engineer,
#: Acme   Mar 2021 - Present`. The leading `\s+` is what makes it trailing.
_TRAILING_DATE_RE = re.compile(rf"\s+({_DATE_BODY})\s*$", re.IGNORECASE)

#: A line that is *nothing but* a date range, which is how most résumés write
#: it — the employer on one line and the dates on the next.
#:
#: Without this the line went through `_render_entry_name`, where the leading
#: `\s+` of the trailing pattern matched the space after the month: `Mar 2021 -
#: Present` split into the name `Mar` and the date `2021 - Present`, and
#: rendered as a bold **Mar** on its own row directly beneath the job title.
#: A bare `2017 - 2021` matched nothing at all and became a bold entry name,
#: reading like an employer. Either way the document showed two entries where
#: there is one job.
_DATE_ONLY_RE = re.compile(rf"^\s*({_DATE_BODY})\s*$", re.IGNORECASE)


def _render_lines(lines: list[str]) -> str:
    items = "\n".join(f"<li>{html.escape(line)}</li>" for line in lines)
    return f"<ul>\n{items}\n</ul>"


def _render_plain(lines: list[str]) -> str:
    items = "\n".join(f"<li>{html.escape(line)}</li>" for line in lines)
    return f"<ul class='plain'>\n{items}\n</ul>"


def _render_paragraphs(lines: list[str]) -> str:
    return "\n".join(f"<p>{html.escape(line)}</p>" for line in lines)


def _render_entry_name(line: str) -> str:
    """An entry name, with any trailing date range split into its own span.

    The date stays *inline* after the name — styled, not floated. Splitting it
    out buys the styling; it must not be read as licence to add `float: right`,
    which was tried and reverted: floating moved the date out of the extracted
    text flow, so `pypdf` returned the entry on one line and its dates
    somewhere else and nothing connected them. See the note on `RESUME_CSS`,
    and `test_a_date_stays_on_the_line_of_the_entry_it_belongs_to`.

    The date is also emitted after the name rather than before it, so a parser
    never reads `Jan 2021 – Present Acme Corp` and has to guess which half is
    the employer.
    """
    match = _TRAILING_DATE_RE.search(line)
    if not match:
        return f"<div class='entry'><span class='entry-name'>{html.escape(line)}</span></div>"
    name = line[: match.start()].rstrip()
    return (
        "<div class='entry'>"
        f"<span class='entry-name'>{html.escape(name)}</span>"
        f"<span class='entry-date'>{html.escape(match.group(1))}</span>"
        "</div>"
    )


def _render_entries(lines: list[str]) -> str:
    """Lay a section out as entries rather than as one undifferentiated list.

    Before this every line in a section was an `<li>` in a `<ul>` with
    `list-style: none` — a project title, its technology line and its bullets
    all rendered identically, with no bullet glyphs anywhere. The document had
    no hierarchy at all, which is most of what "the résumé has no format"
    means.

    Grouping uses `packages.tailor.bullets.classify`, the same function that
    decides what the model is asked to rewrite. One answer to "what is this
    line", so the document cannot be laid out on one reading of it and
    tailored on another.
    """
    from packages.tailor.bullets import LineKind, classify

    parts: list[str] = []
    pending_bullets: list[str] = []
    #: Index of the entry a bare date line would belong to, or None when there
    #: is no open entry — the date of a job is written under its title, so once
    #: bullets have been emitted the entry above is finished.
    open_entry: int | None = None

    def flush() -> None:
        nonlocal open_entry
        if pending_bullets:
            parts.append(_render_lines(pending_bullets))
            pending_bullets.clear()
            open_entry = None

    for line in lines:
        if not line.strip():
            continue
        kind = classify(line)
        if kind is LineKind.BULLET:
            pending_bullets.append(line)
            continue
        flush()

        date_only = _DATE_ONLY_RE.match(line)
        if date_only is not None:
            if open_entry is not None:
                parts[open_entry] = _with_date(parts[open_entry], date_only.group(1))
                continue
            # No entry to attach to. Still not a name: rendering a date in bold
            # as though it were an employer is the failure this branch avoids.
            parts.append(f"<p class='entry-meta'>{html.escape(line)}</p>")
            continue

        if kind is LineKind.ENTRY:
            parts.append(_render_entry_name(line))
            open_entry = len(parts) - 1
        else:
            parts.append(f"<p class='entry-meta'>{html.escape(line)}</p>")

    flush()
    return "\n".join(parts)


def _with_date(entry_html: str, date: str) -> str:
    """Attach a date to an already-rendered entry.

    A no-op when the entry carries one already: a résumé that writes the dates
    beside the title *and* on the line below has said it twice, and printing
    both is worse than either.
    """
    if "entry-date" in entry_html:
        return entry_html
    closing = "</div>"
    if not entry_html.endswith(closing):
        return entry_html
    return (
        entry_html[: -len(closing)]
        + f"<span class='entry-date'>{html.escape(date)}</span>"
        + closing
    )


def _render_body(name: str, lines: list[str]) -> str:
    """The lines of one section, laid out the way that section reads."""
    if name == "summary":
        return _render_paragraphs(lines)
    if name in _PLAIN_SECTIONS:
        return _render_plain(lines)
    if name in _ENTRY_SECTIONS:
        return _render_entries(lines)
    return _render_lines(lines)


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
            # `replace_source_projects` was dead here. This branch marked
            # "projects" emitted whenever the GitHub section rendered, so the
            # trailing loop that honours the flag could never reach it — and a
            # résumé whose Projects section *is* its substance had that section
            # silently replaced by repository metadata, tailored rewrites and
            # all. Setting the flag to False changed nothing.
            source_projects = resume.section("projects")
            keep_source = bool(source_projects) and (
                not rendered_projects or not opts.replace_source_projects
            )
            if rendered_projects:
                parts.append(rendered_projects)
                emitted.add("projects")
            if keep_source:
                heading = "Projects" if not rendered_projects else "Selected Project Experience"
                parts.append(
                    f"<section><h2>{html.escape(heading)}</h2>"
                    f"{_render_body('projects', source_projects)}</section>"
                )
                emitted.add("projects")
            continue

        lines = resume.section(name)
        if not lines:
            continue
        title = SECTION_TITLES.get(name, name.title())
        parts.append(f"<section><h2>{html.escape(title)}</h2>{_render_body(name, lines)}</section>")
        emitted.add(name)

    # A section the parser found but the layout does not know about is kept,
    # not dropped — losing content from someone's résumé is the worse failure.
    for name, lines in resume.sections.items():
        if name in emitted or not lines:
            continue
        if name == "projects" and opts.replace_source_projects and rendered_projects:
            continue
        title = SECTION_TITLES.get(name, name.title())
        parts.append(f"<section><h2>{html.escape(title)}</h2>{_render_body(name, lines)}</section>")

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
