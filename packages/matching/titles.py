"""Expand the abbreviations job titles are written in.

Measured against `LexicalEmbedder`, before this existed:

    Site Reliability Engineer  vs  SRE        0.000
    Software Engineer II       vs  SDE 2      0.000
    Senior Backend Engineer    vs  Pastry Chef 0.000

The same job scored exactly as similar as an unrelated one, because an
abbreviation shares no tokens with what it abbreviates. That is not a
semantics problem an embedding solves — "SRE" and "site reliability" do not
co-occur often enough in general text for a pretrained vector to place them
together, and a model large enough to know would be a gigabyte-scale download
to answer a question a lookup table answers exactly.

## What belongs in the table

Only expansions with one reading in a hiring context. "PM" is product manager
or project manager depending on the company, and guessing turns a title into
the wrong job rather than a fuzzy one — so it is absent. An abbreviation that
needs a coin flip is left as it is.

## Expansion adds, never replaces

`SRE` becomes `SRE site reliability engineer`. Dropping the original would
lose the match against a posting that also writes `SRE`, which is the more
common case. Both forms present means both match.
"""

from __future__ import annotations

import re

#: Abbreviation -> what it stands for. Lowercased keys; matching is
#: case-insensitive and whole-word.
ALIASES: dict[str, str] = {
    "sre": "site reliability engineer",
    "sde": "software development engineer",
    "swe": "software engineer",
    "ml": "machine learning",
    "ai": "artificial intelligence",
    "nlp": "natural language processing",
    "qa": "quality assurance",
    "ui": "user interface",
    "ux": "user experience",
    "devops": "development operations",
    "mlops": "machine learning operations",
    "dba": "database administrator",
    "ba": "business analyst",
    "cto": "chief technology officer",
    "cio": "chief information officer",
    "vp": "vice president",
    "sr": "senior",
    "jr": "junior",
    "eng": "engineer",
    "dev": "developer",
    "arch": "architect",
    "infra": "infrastructure",
    "sec": "security",
    "fe": "frontend",
    "be": "backend",
    "fs": "full stack",
    "gis": "geographic information systems",
    "etl": "extract transform load",
    "bi": "business intelligence",
}

#: Roman numerals as titles use them. "Engineer II" and "Engineer 2" are one
#: level; leaving them different splits a rung in two.
_NUMERALS: dict[str, str] = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5"}

#: A word, or an abbreviation with dots — "S.R.E." appears in the wild.
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z.]*|\d+")


def _bare(word: str) -> str:
    return word.replace(".", "").lower()


def expand(title: str | None, *, keep_original: bool = True) -> str:
    """`title` with abbreviations spelled out.

    `keep_original` decides whether the abbreviation survives beside its
    expansion. For embedding it must — a posting that also writes "SRE" is the
    common case, and both forms present means both match. For an equality
    check it must not, or "Sr. SWE" and "Senior Software Engineer" compare
    unequal on the abbreviations alone.

    Returns the empty string for nothing, so callers can pass a nullable
    column straight through.
    """
    if not title:
        return ""

    parts: list[str] = []
    for match in _WORD_RE.finditer(title):
        word = match.group(0)
        bare = _bare(word)

        # A roman numeral is *replaced*, not added to: "II" and "2" are two
        # spellings of one thing, and keeping both would make "Engineer II"
        # look like it mentions two levels.
        if bare in _NUMERALS:
            parts.append(_NUMERALS[bare])
            continue

        expansion = ALIASES.get(bare)
        if expansion is None:
            parts.append(word)
            continue

        if keep_original:
            parts.append(word)
        parts.append(expansion)

    return " ".join(parts)


def canonical(title: str | None) -> str:
    """A comparable form, for deciding whether two titles are the same role.

    Lowercased, abbreviations expanded, duplicates removed, sorted. Unlike
    `expand` this is not for embedding — it is for equality, where word order
    is noise: "Engineer, Backend" and "Backend Engineer" name one job.
    """
    words = expand(title, keep_original=False).lower().split()
    return " ".join(sorted({word.strip(".,;:") for word in words if word.strip(".,;:")}))
