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

from packages.tailor.rewrite import TailorResult


class LineChange(BaseModel):
    original: str
    tailored: str
    #: Word-level markup, for showing what moved inside a line.
    inline_html: str = ""


class DiffSummary(BaseModel):
    changes: list[LineChange] = Field(default_factory=list)
    unchanged: int = 0
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


def summarize(result: TailorResult) -> DiffSummary:
    """Turn a tailoring run into something reviewable."""
    changes = [
        LineChange(
            original=bullet.original,
            tailored=bullet.tailored,
            inline_html=inline_html(bullet.original, bullet.tailored),
        )
        for bullet in result.bullets
        if bullet.changed
    ]

    return DiffSummary(
        changes=changes,
        unchanged=len(result.bullets) - len(changes),
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
