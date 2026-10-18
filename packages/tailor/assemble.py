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
from dataclasses import replace

import structlog
from pydantic import BaseModel, Field

from packages.core.models import Project
from packages.tailor.dates import date_only, trailing_date
from packages.tailor.parse import ParsedResume
from packages.tailor.projects import (
    LinkStyle,
    render_section_html,
)
from packages.tailor.style import (
    FONT_FACES,
    Rendered,
    ResumeFont,
    ResumeStyle,
    fit_to_pages,
    page_count,
    resume_css,
    uses_face,
)

log = structlog.get_logger(__name__)

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
#:
#: Three rules say where a page may not break. An entry's name stays with the
#: line under it, a line of supporting detail with the bullets it introduces,
#: and a bullet is not cut in two. Without them an employer's name was the
#: last line of page one with its dates and degree on page two
#: (`tests/test_resume_page_layout.py`). A break may still fall between any
#: two bullets, so no entry is one block that has to move whole and leave a
#: hole behind it. A section heading needs no rule: WeasyPrint's own
#: stylesheet keeps `h2` with what follows it.
#:
#: The face and the sizes are `packages/tailor/style.py`'s. This is the
#: template at its defaults, which is the document every layout test measured.
RESUME_CSS = resume_css(ResumeStyle())


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
    #: The face and sizes. The default is the template as it has always been.
    style: ResumeStyle = Field(default_factory=ResumeStyle)
    #: Tighten the type within `style.FIT_FLOOR` when a few lines spill onto a
    #: last page. Off here so a bare render is the template; tailoring turns it
    #: on from settings (`publish.house_options`).
    fit: bool = False
    #: Fit into at most this many pages, when the floor allows it.
    max_pages: int | None = Field(default=None, ge=1)


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
#:
#: `certifications`, `publications` and `awards` are entry-shaped for the same
#: reason the first three are: each item is a name, usually a date, and
#: sometimes a line of detail. Left out, they rendered as an undifferentiated
#: bullet list — the certificate, the date it was issued and its detail all as
#: identical rows, which is the exact hierarchy failure `_render_entries` was
#: written to fix, surviving in the sections nobody had looked at.
_ENTRY_SECTIONS: frozenset[str] = frozenset(
    {"experience", "projects", "education", "certifications", "publications", "awards"}
)

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
#: Dates come from `packages/tailor/dates.py`, which is the only definition
#: of what one looks like. This module used to carry its own, and it and the
#: ATS scorer disagreed about `Summer 2024` — see that module's docstring for
#: what the disagreement cost.


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
    split = trailing_date(line)
    if split is None:
        return f"<div class='entry'><span class='entry-name'>{html.escape(line)}</span></div>"
    name, date = split
    return (
        "<div class='entry'>"
        f"<span class='entry-name'>{html.escape(name)}</span>"
        f"<span class='entry-date'>{html.escape(date)}</span>"
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

        bare_date = date_only(line)
        if bare_date is not None:
            if open_entry is not None:
                parts[open_entry] = _with_date(parts[open_entry], bare_date)
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


def render_resume(
    resume: ParsedResume,
    projects: list[Project] | None = None,
    options: AssemblyOptions | None = None,
) -> Rendered:
    """Render the assembled résumé, set in its face and fitted if asked.

    A face the PDF does not embed is not used. fontconfig answers a missing
    family with whatever it likes, so the document is set in the default face
    instead, and the note says which face was asked for.
    """
    from weasyprint import CSS, HTML

    opts = options or AssemblyOptions()
    body = assemble_html(resume, projects, opts)
    document = HTML(string=f"<body>{body}</body>")

    def render(style: ResumeStyle) -> bytes:
        return bytes(document.write_pdf(stylesheets=[CSS(string=resume_css(style))]))

    style = opts.style
    first = render(style)
    missing_face: str | None = None
    if not uses_face(first, style.font):
        missing_face = FONT_FACES[style.font].label
        log.warning("resume_face_unavailable", font=str(style.font))
        style = style.model_copy(update={"font": ResumeFont.DEFAULT})
        first = render(style)

    if opts.fit:
        rendered = fit_to_pages(render, style, max_pages=opts.max_pages, first=first)
    else:
        count = page_count(first)
        rendered = Rendered(pdf=first, style=style, pages=count, pages_before=count)
    if missing_face is None:
        return rendered
    said = f"{missing_face} is not installed here, so the résumé is set in the default face."
    return replace(rendered, note=f"{said} {rendered.note}" if rendered.note else said)


def assemble_pdf(
    resume: ParsedResume,
    projects: list[Project] | None = None,
    options: AssemblyOptions | None = None,
) -> bytes:
    """Render the assembled résumé to PDF bytes."""
    return render_resume(resume, projects, options).pdf


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
