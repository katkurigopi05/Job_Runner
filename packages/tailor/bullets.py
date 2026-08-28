"""Which section of a résumé the tailorer rewrites.

Every caller used to answer this by writing `section("experience")` inline —
the extractor in `apply_job`, the one in `batch`, the one in `compare`, and the
write-back in `publish`. Four copies of a decision that must agree, and
`publish.TAILORED_SECTION` already carried a note saying so.

They agreed on the wrong answer for a whole class of résumé. A student, a new
graduate, or a career changer has no employment section: the substance is under
Projects. `section("experience")` returns nothing, `_tailor` returns `None`, and
the entire Phase 3 pipeline is a silent no-op — the owner sees a review screen
with no diff and no reason given, and the employer receives the base résumé.

## Why Projects is a safe fallback

§2.1 is *more* permissive there, not less: the Projects section may carry facts
verified by GitHub's source-reported name, description, language and topics,
provided they stay attributed to that project. Rewriting a project bullet is the
same operation as rewriting an employment bullet and is held to the same guard —
and `guard._ATTRIBUTED_SECTIONS` already lists `projects`, so a rewrite is
scoped to its own project and cannot borrow from a sibling.

## Experience wins when both exist

Order matters and is not alphabetical. A résumé with both sections is an
employment résumé whose projects are supporting material; rewriting the projects
and leaving the jobs untouched would tailor the half the employer reads second.
"""

from __future__ import annotations

from packages.tailor.parse import ParsedResume

#: Sections the tailorer may rewrite, in priority order.
TAILORABLE_SECTIONS: tuple[str, ...] = ("experience", "projects")


def tailorable_section(parsed: ParsedResume) -> str | None:
    """The section holding the bullets to rewrite, or None if there are none.

    None means there is nothing to tailor — not that something failed. A résumé
    with neither section is one the parser could not read, or one that genuinely
    has no prose to rewrite, and both are the caller's to report.
    """
    for name in TAILORABLE_SECTIONS:
        if any(line.strip() for line in parsed.section(name)):
            return name
    return None


def tailorable_bullets(parsed: ParsedResume) -> tuple[str | None, list[str]]:
    """The section name and its non-empty lines.

    Returned together so a caller cannot rewrite bullets taken from one section
    and write them back into another — the failure this module exists to make
    impossible.
    """
    name = tailorable_section(parsed)
    if name is None:
        return None, []
    return name, [line for line in parsed.section(name) if line.strip()]
