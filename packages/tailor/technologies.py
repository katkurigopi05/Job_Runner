"""The technologies a résumé lists for itself, and whether a rewrite kept them.

The fabrication guard asks one question — is everything in this rewrite
supported by the source? A rewrite can pass it and still damage the document,
by *removing* something true. Both models tested on the owner's résumé did
exactly that, on the same bullet:

    Built a companion Python application (Pillow/Tkinter) with 35
    non-destructive editing operations, ...
      -> Built a companion Python application with 35 non-destructive
         editing operations, ...

Nothing was invented, so nothing was refused, and the résumé came out of
tailoring with two fewer library names on it. On a screen whose purpose is
improving the match against a posting, a rewrite that deletes the exact tokens
an ATS scans for is worse than no rewrite at all — and it is invisible, because
every check in `guard.py` reads the output and none of them reads what is
missing from it.

## Why only listed technologies

Refusing *any* dropped proper noun is far too strict: `extract_entities` reads
`Designing`, `Filtered`, `Provisioned` and `Added` as proper nouns because they
open a sentence, and the fragments of a multiword name — `Forest`, `Logistic`,
`Studio` — come through as separate entities too. A rewrite that says `Curated`
where the source said `Filtered` is ordinary re-emphasis and §2.1 permits it.

So the protected set is not "names" but "technologies this résumé claims", read
off the lines where the owner listed them: the skills section, and the stack
lines under each project. Those are the terms the document is asserting as
skills, and dropping one silently contradicts the rest of the page.
"""

from __future__ import annotations

import re

from packages.tailor.bullets import LineKind, classify, tailorable_section
from packages.tailor.guard import _COMMON_WORDS, _TOKEN_RE, _index, normalize, singular
from packages.tailor.parse import ParsedResume

#: Sections that list technologies rather than describe work.
_SKILL_SECTIONS = ("skills",)

#: Separators inside one list fragment: `GitHub Actions (CI/CD)` names GitHub
#: Actions, CI and CD; `WebGPU/WASM` names two. Split so each is protected on
#: its own, because a rewrite drops them one at a time.
_FRAGMENT_SPLIT_RE = re.compile(r"[(),/]")

#: A single letter is not a technology worth protecting even when the résumé
#: lists one — `C` and `R` are real languages, but they also appear as initials,
#: section markers and list bullets, and a false refusal costs a rewrite.
_MIN_TERM_LENGTH = 2


def inventory(parsed: ParsedResume) -> frozenset[str]:
    """Normalized technology names the résumé lists for itself.

    Read from the skills section and from the stack lines under the section
    being tailored — `classify` already separates those from prose, so this
    asks it rather than inventing a second notion of "not a bullet".
    """
    lines: list[str] = []
    for name in _SKILL_SECTIONS:
        lines.extend(parsed.section(name))

    section = tailorable_section(parsed)
    if section is not None:
        lines.extend(line for line in parsed.section(section) if classify(line) is LineKind.META)

    terms: set[str] = set()
    for line in lines:
        for fragment in _FRAGMENT_SPLIT_RE.split(line):
            for match in _TOKEN_RE.finditer(fragment):
                term = normalize(match.group(0))
                if len(term) < _MIN_TERM_LENGTH or term in _COMMON_WORDS:
                    continue
                if term.isdigit():
                    continue
                terms.add(term)
    return frozenset(terms)


def dropped(original: str, candidate: str, listed: frozenset[str]) -> tuple[str, ...]:
    """Listed technologies the original names and the candidate does not.

    Presence in the candidate is decided by `guard._index`, so an equivalent
    spelling counts as keeping the term — a rewrite saying `PostgreSQL` where
    the source said `Postgres` has not dropped anything, and refusing it would
    punish the one substitution the alias table exists to allow.
    """
    if not listed:
        return ()

    # Both sides are split on the same separators the inventory was built with.
    # Without it the check misses the case that motivated it: `_TOKEN_RE` reads
    # `Pillow/Tkinter` as a single token, so the original yields
    # `pillow/tkinter` — which matches no inventory entry — and a rewrite that
    # deleted both names looked clean.
    present = _index(_separated(candidate))
    lost: list[str] = []
    seen: set[str] = set()

    for match in _TOKEN_RE.finditer(_separated(original)):
        token = match.group(0)
        term = normalize(token)
        if term not in listed or term in seen:
            continue
        if term in present or singular(term) in present:
            continue
        seen.add(term)
        lost.append(token)

    return tuple(lost)


def _separated(text: str) -> str:
    """`text` with compound separators turned into spaces."""
    return _FRAGMENT_SPLIT_RE.sub(" ", text)
