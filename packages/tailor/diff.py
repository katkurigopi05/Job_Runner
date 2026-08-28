"""Diff a tailored résumé against its source.

CLAUDE.md §9, Gate 3: the diff renders before anything is sent. The point is
that the owner can see every change at a glance and refuse the ones they do
not recognize — which is also the last line of defence behind the fabrication
guard.
"""

from __future__ import annotations

import difflib
import html

from pydantic import BaseModel, Field

from packages.tailor.parse import ParsedResume
from packages.tailor.rewrite import TailorResult


class LineChange(BaseModel):
    original: str
    tailored: str
    #: Word-level markup, for showing what moved inside a line.
    inline_html: str = ""


class AtsChange(BaseModel):
    """The ATS score before and after this tailoring run.

    On the review record rather than computed by the screen, because the screen
    would have to re-parse both documents to ask — and because the answer is a
    property of the run that produced this diff, not of whenever someone
    happens to look at it.

    Both halves are carried. A run that raises keyword coverage while lowering
    the parse score has made the document worse in the way that matters most,
    and one number would hide it.
    """

    parse_before: float = 0.0
    parse_after: float = 0.0
    keywords_before: float = 0.0
    keywords_after: float = 0.0
    #: Posting terms the tailored résumé backs that the source did not surface.
    gained: list[str] = Field(default_factory=list)
    #: Terms the posting asks for and the résumé still cannot back. Not a
    #: to-do list — writing one in would be fabrication.
    still_missing: list[str] = Field(default_factory=list)

    @property
    def parse_regressed(self) -> bool:
        return self.parse_after < self.parse_before


class DiffSummary(BaseModel):
    changes: list[LineChange] = Field(default_factory=list)
    unchanged: int = 0
    #: None when the run had no posting text to score against.
    ats: AtsChange | None = None
    #: Rewrites the fabrication guard refused — a statement about what the model
    #: tried to write.
    rejected: int = 0
    #: Bullets where the model never answered — a statement about the network.
    #: Kept apart from `rejected` because a comparison that adds them together
    #: makes a provider that was down look like one that kept inventing.
    provider_failures: int = 0
    unified: str = ""

    @property
    def changed(self) -> int:
        return len(self.changes)

    @property
    def is_empty(self) -> bool:
        return not self.changes


def unified(original: list[str], tailored: list[str], *, context: int = 1) -> str:
    """Standard unified diff, for anyone who wants to read it as text."""
    return "\n".join(
        difflib.unified_diff(
            original,
            tailored,
            fromfile="resume (source)",
            tofile="resume (tailored)",
            lineterm="",
            n=context,
        )
    )


def inline_html(original: str, tailored: str) -> str:
    """Word-level diff of one line, as `<del>`/`<ins>` markup."""
    matcher = difflib.SequenceMatcher(a=original.split(), b=tailored.split())
    parts: list[str] = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        before = html.escape(" ".join(original.split()[i1:i2]))
        after = html.escape(" ".join(tailored.split()[j1:j2]))
        match tag:
            case "equal":
                parts.append(before)
            case "delete":
                parts.append(f"<del>{before}</del>")
            case "insert":
                parts.append(f"<ins>{after}</ins>")
            case "replace":
                parts.append(f"<del>{before}</del> <ins>{after}</ins>")

    return " ".join(p for p in parts if p)


def summarize(
    result: TailorResult,
    *,
    source: ParsedResume | None = None,
    tailored: ParsedResume | None = None,
    job_description: str = "",
) -> DiffSummary:
    """Turn a tailoring run into something reviewable.

    With both résumés and the posting, the ATS score before and after is
    computed and carried too. Optional because two callers have a
    `TailorResult` and nothing else — the module's own tests, and the
    comparison panel, which scores its sides itself.
    """
    changes = [
        LineChange(
            original=bullet.original,
            tailored=bullet.tailored,
            inline_html=inline_html(bullet.original, bullet.tailored),
        )
        for bullet in result.bullets
        if bullet.changed
    ]

    ats: AtsChange | None = None
    if source is not None and tailored is not None and job_description.strip():
        from packages.tailor.ats import score_change

        delta = score_change(source, tailored, job_description)
        ats = AtsChange(
            parse_before=delta.parse_before,
            parse_after=delta.parse_after,
            keywords_before=delta.keywords_before,
            keywords_after=delta.keywords_after,
            gained=delta.gained,
            still_missing=delta.still_missing[:15],
        )

    return DiffSummary(
        changes=changes,
        unchanged=len(result.bullets) - len(changes),
        ats=ats,
        rejected=result.rejected,
        provider_failures=result.provider_failures,
        unified=unified([b.original for b in result.bullets], [b.tailored for b in result.bullets]),
    )


DIFF_CSS = """
.diff li { margin: 0 0 6pt; }
.diff del { background: #ffe3e3; text-decoration: line-through; }
.diff ins { background: #e3f7e3; text-decoration: none; }
"""


def render_html(summary: DiffSummary) -> str:
    """The review view. Empty string when nothing changed."""
    if summary.is_empty:
        return ""
    items = "\n".join(f"<li>{c.inline_html}</li>" for c in summary.changes)
    return f'<section class="diff"><h2>Proposed changes</h2><ul>{items}</ul></section>'
