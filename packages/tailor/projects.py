"""Render a Projects section for a résumé, with linked project references.

## Why the link style is configurable

A link icon alone is invisible to an ATS. Measured: rendering
`<a href="https://github.com/you/repo">🔗</a>` to PDF puts the URL in the
document's *link annotation*, which browsers follow — but `extract_text()`,
which is what a résumé parser reads, returns only the icon glyph. Ship
icon-only and the recruiter's system stores a project with no link at all.

So there are three styles, and the default keeps the URL in the text layer:

- `ICON_SLUG` (default) — a link icon plus a compact `github.com/you/repo`.
  Clickable for a human, readable by a parser, short enough not to clutter.
- `ICON_ONLY` — icon alone. Prettiest, and the link is lost on any ATS that
  reads text rather than annotations. Opt in knowing that.
- `FULL_URL` — the whole `https://…`. Ugly, maximally parseable.

What this module will not do is put the URL in the text layer as invisible or
white-on-white text. That is the classic ATS keyword-stuffing trick, it is
detectable, and it misrepresents the document — the same reason §2.5 rules out
bot-detection evasion.

All text rendered here comes from the project record verbatim. Descriptions
are never generated: a project with none renders with none.
"""

from __future__ import annotations

import html
from enum import StrEnum
from urllib.parse import urlparse

from pydantic import BaseModel

from packages.core.models import Project

#: U+1F517 LINK SYMBOL. Kept as a named constant so the glyph is swappable for
#: an SVG without hunting through templates.
LINK_ICON = "\N{LINK SYMBOL}"


class LinkStyle(StrEnum):
    ICON_SLUG = "icon_slug"
    ICON_ONLY = "icon_only"
    FULL_URL = "full_url"


class ProjectEntry(BaseModel):
    """One rendered project line, source-faithful."""

    name: str
    description: str | None
    url: str
    #: `github.com/owner/repo` — the compact, parseable form.
    slug: str
    language: str | None
    topics: list[str]

    @classmethod
    def from_project(cls, project: Project) -> ProjectEntry:
        return cls(
            name=project.name,
            description=project.description,
            url=project.url,
            slug=compact_slug(project.url),
            language=project.language,
            topics=list(project.topics_json or []),
        )


def compact_slug(url: str) -> str:
    """`https://github.com/you/repo` → `github.com/you/repo`."""
    parsed = urlparse(url)
    host = parsed.netloc.removeprefix("www.")
    path = parsed.path.rstrip("/")
    return f"{host}{path}" if host else url


def link_text(entry: ProjectEntry, style: LinkStyle) -> str:
    """The visible text of the anchor, per style."""
    match style:
        case LinkStyle.ICON_ONLY:
            return LINK_ICON
        case LinkStyle.FULL_URL:
            return entry.url
        case _:
            return f"{LINK_ICON} {entry.slug}"


def render_entry_html(entry: ProjectEntry, style: LinkStyle = LinkStyle.ICON_SLUG) -> str:
    """One project as an HTML list item."""
    parts = [f'<span class="project-name">{html.escape(entry.name)}</span>']

    if entry.description:
        parts.append(f'<span class="project-desc">{html.escape(entry.description)}</span>')

    anchor = (
        f'<a class="project-link" href="{html.escape(entry.url, quote=True)}">'
        f"{html.escape(link_text(entry, style))}</a>"
    )
    parts.append(anchor)

    return "<li>" + " ".join(parts) + "</li>"


def render_section_html(
    projects: list[Project],
    *,
    style: LinkStyle = LinkStyle.ICON_SLUG,
    heading: str = "Projects",
) -> str:
    """The whole Projects section. Empty string when there is nothing to show.

    An empty section is omitted rather than rendered with a heading and no
    content — a résumé should not advertise a gap.
    """
    if not projects:
        return ""

    entries = [ProjectEntry.from_project(p) for p in projects]
    items = "\n".join(render_entry_html(e, style) for e in entries)
    return (
        f'<section class="projects">\n'
        f"<h2>{html.escape(heading)}</h2>\n"
        f"<ul>\n{items}\n</ul>\n"
        f"</section>"
    )


#: Minimal print styling. Deliberately plain: no columns, no tables, no text
#: boxes — all of which break ATS text extraction.
SECTION_CSS = """
@page { size: Letter; margin: 0.6in; }
body { font-family: "DejaVu Sans", Helvetica, Arial, sans-serif;
       font-size: 10.5pt; line-height: 1.35; color: #111; }
h2 { font-size: 12pt; margin: 0 0 6pt; text-transform: uppercase;
     letter-spacing: 0.04em; border-bottom: 0.6pt solid #999; padding-bottom: 2pt; }
ul { list-style: none; padding: 0; margin: 0; }
li { margin: 0 0 6pt; }
.project-name { font-weight: 700; }
.project-desc { }
.project-link { color: #1a4d8f; text-decoration: none; white-space: nowrap; }
"""


def render_section_pdf(
    projects: list[Project],
    *,
    style: LinkStyle = LinkStyle.ICON_SLUG,
    heading: str = "Projects",
) -> bytes:
    """Render the section to PDF bytes.

    Split out from résumé assembly so the link behaviour can be round-tripped
    through a parser on its own — see tests/test_projects.py.
    """
    from weasyprint import CSS, HTML

    body = render_section_html(projects, style=style, heading=heading)
    document = HTML(string=f"<body>{body}</body>")
    return bytes(document.write_pdf(stylesheets=[CSS(string=SECTION_CSS)]))
