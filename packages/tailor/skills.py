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

## What counts as one skill

A label is either text ending in a colon or, for a Skills section laid out as
a table, the first cell: the parser joins a row's cells with two spaces, so
`Frameworks & Web  FastAPI, React` is a label and two skills. A bracketed list
belongs to the skill before it: `Generative AI (VAE, GAN)` is one skill, and
a comma inside the brackets does not split it.

Missing either one scrambled every résumé tailored from a table-laid Skills
section: labels moved mid-line ("React, Frameworks & Web  FastAPI") and
bracketed lists were split and their pieces moved apart ("LLM Orchestration
(OpenAI, Deep Learning, GAN, …"). A line whose brackets do not balance is
returned as it was, because its groups cannot be read.
"""

from __future__ import annotations

import re

from packages.matching.embed import tokenize

#: Separators a skills line actually uses. A line with none of them is prose,
#: not a list, and is returned untouched.
_SEPARATORS = frozenset(",;|•·")

#: A separator inside brackets belongs to the skill the brackets follow.
_OPENERS = frozenset("([{")
_CLOSERS = frozenset(")]}")

#: "Frameworks & Web  FastAPI, React": a table row, label cell and value cell
#: joined with two spaces by the parser. Found by the gap rather than by its
#: contents, because a label can hold a comma ("Data, DevOps & Tools").
_GAP_LABEL = re.compile(r"^(\S.{0,39}?)(\t\s*|\s{2,})(\S.*)$")

#: "Languages: Go, Java" — the part before the colon names the group and is not
#: itself a skill. Anchored and bounded so a colon inside a skill ("Ruby: on
#: Rails") cannot swallow half the line.
_COLON_LABEL = re.compile(r"^([^:]{1,40}:)(\s*)(.+)$")


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

    label, gap, body = _split_label(line)
    parts = _split_skills(body)
    if parts is None or len(parts) < 2:
        # Unreadable brackets, prose, or a single skill. In each case there is
        # nothing safe to reorder, and rewriting it would risk changing text
        # this module must not touch.
        return line

    asked = [p for p in parts if _asked_for(p, wanted)]
    rest = [p for p in parts if not _asked_for(p, wanted)]
    return label + gap + ", ".join(asked + rest)


def _split_label(line: str) -> tuple[str, str, str]:
    """Return (label, gap, skills). The gap is kept exactly as written.

    A label never ends in a separator, so a stray double space in "Java,  Go"
    is not read as a label called "Java,". Nor does it leave a bracket open,
    so "Cloud (AWS: EC2, S3)" is not cut at its colon.
    """
    for pattern in (_GAP_LABEL, _COLON_LABEL):
        match = pattern.match(line)
        if not match:
            continue
        label = match.group(1)
        if label.rstrip()[-1] not in _SEPARATORS and _split_skills(label) is not None:
            return label, match.group(2), match.group(3)
    body = line.lstrip()
    return "", line[: len(line) - len(body)], body


def _split_skills(text: str) -> list[str] | None:
    """Split at separators outside brackets; None when the brackets do not balance."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for char in text:
        if char in _OPENERS:
            depth += 1
        elif char in _CLOSERS:
            depth -= 1
            if depth < 0:
                return None
        if char in _SEPARATORS and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if depth:
        return None
    parts.append("".join(current).strip())
    return parts
