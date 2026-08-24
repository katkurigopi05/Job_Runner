"""Order the Skills section by what a posting asks for.

§2.1 permits a rewrite to "rephrase, reorder, re-emphasize". This does only the
third: every skill on the source résumé appears on the tailored one, spelled
exactly as the owner spelled it, and only the order changes. Nothing here can
add a skill, because nothing here writes text — it partitions a list.

## Why it does not drop the rest

Removing unrelated skills makes a shorter, sharper page for a human, and costs
an ATS keyword scan that nobody can observe. A recruiter filtering on a skill
that was dropped does not produce a rejection to learn from; it produces
silence. Re-emphasis buys the readable half of that benefit and risks nothing,
so the unmatched skills move down rather than off.

## Why lines are never reordered against each other

A Skills section is usually labelled groups — "Languages:", "Databases:",
"Cloud:". Sorting whole lines by relevance would put Tools above Languages and
restructure the résumé rather than re-emphasize it. Reordering happens strictly
inside a line, and the line's own label stays at its front.
"""

from __future__ import annotations

import re

from packages.matching.embed import tokenize

#: Separators a skills line actually uses. A line with none of them is prose,
#: not a list, and is returned untouched.
_SEPARATOR = re.compile(r"\s*[,;|•·]\s*")

#: "Languages: Go, Java" — the part before the colon names the group and is not
#: itself a skill. Anchored and bounded so a colon inside a skill ("Ruby: on
#: Rails") cannot swallow half the line.
_LABEL = re.compile(r"^([^:]{1,40}:)\s*(.+)$")


def _asked_for(skill: str, wanted: set[str]) -> bool:
    """Whether the posting asks for this skill.

    Token-level so "Kubernetes Operators" matches a posting that says
    Kubernetes, and so punctuation and case cannot decide the answer.
    """
    return any(token in wanted for token in tokenize(skill))


def reorder_skills(lines: list[str], job_text: str) -> list[str]:
    """Move the skills this posting names to the front of their own line.

    Order-only and stable: within both the asked-for group and the remainder,
    the owner's original sequence survives. That sequence is information —
    people list their strongest first — and a sort would discard it.
    """
    if not job_text.strip():
        return list(lines)

    wanted = set(tokenize(job_text))
    if not wanted:
        return list(lines)

    return [_reorder_line(line, wanted) for line in lines]


def _reorder_line(line: str, wanted: set[str]) -> str:
    if not line.strip():
        return line

    label = ""
    body = line
    match = _LABEL.match(line)
    if match:
        label, body = match.group(1), match.group(2)

    parts = _SEPARATOR.split(body)
    if len(parts) < 2:
        # Prose, or a single skill. Either way there is nothing to reorder, and
        # rewriting it would risk changing text this module must not touch.
        return line

    asked = [p for p in parts if _asked_for(p, wanted)]
    rest = [p for p in parts if not _asked_for(p, wanted)]
    reordered = ", ".join(asked + rest)

    return f"{label} {reordered}" if label else reordered
