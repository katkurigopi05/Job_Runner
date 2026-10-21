"""Protect facts stated inside a bullet, including names absent from Skills.

This is a lexical loss check, not general entailment. Names, technical
compounds and short enumerated items carry facts; an opening action or an
ordinary adjective is free to change. The alias index decides what survives.
"""

from __future__ import annotations

import re

from packages.tailor.guard import _COMMON_WORDS, _TOKEN_RE, normalize

_COMPOUND = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+$")
_COMMA = re.compile(r",\s+")  # Never split the count in `2,338`.
_CONJUNCTION = re.compile(r"^(?:and|or)\s+")
_INTRODUCER = re.compile(r"\b(?:for|with|including|checking|to)\s+")
_QUALIFIER = re.compile(r"\s+\b(?:via|with|for|using|across|that|so|to)\b")
_CLAUSE = re.compile(r"^\w+(?:ed|ing)\s+", re.IGNORECASE)
_GROUP = re.compile(r"\(([^()]*)\)")
_LABELLED_NAME = re.compile(r"\b([A-Za-z][\w.+/-]*)\s+(?:library|package|SDK|model|product)\b")


def _items(original: str, *, sentence: bool = True) -> list[str]:
    """Short comma-list items, not whole subordinate clauses.

    Require a series or a closing conjunction. Keep the first item after its
    introducer and the last before its qualifier, so `for editor state, ...,
    and motion tracking` protects both ends. Long/complex clauses are left to
    the existing guard rather than treating every missing word as a fact.
    """
    # Read parenthesized enumerations in their own scope. Otherwise the first
    # model name gets attached to the prose before `(`, protecting filler too.
    groups = _GROUP.findall(original)
    items = [item for group in groups for item in _items(group, sentence=False)]
    parts = _COMMA.split(_GROUP.sub("", original))
    if len(parts) < 3 and not (len(parts) == 2 and _CONJUNCTION.match(parts[-1])):
        return items
    for index, part in enumerate(parts):
        # A comma inside brackets is not a boundary in the surrounding prose.
        # Names/compounds in that group are independently protected below.
        if any(char in part for char in "()[]{}"):
            continue
        part = _CONJUNCTION.sub("", part).strip(" .;()")
        if index == 0 and sentence:
            # The sentence's action is rephrasable, never a list item.
            part = _INTRODUCER.split(part)[-1]
            if part == parts[0].strip(" .;()"):
                part = part.partition(" ")[2]
        elif _CLAUSE.match(part):
            continue
        part = _QUALIFIER.split(part)[0].strip(" .;()")
        if 0 < len(part.split()) <= 4:
            items.append(part)
    return items


def dropped(original: str, candidate: str) -> tuple[str, ...]:
    """Bullet-local facts missing from `candidate`, as the bullet wrote them."""
    # targeted imports rewrite.vet. Import at call time to share its existing
    # alias/morphology-aware loss check without introducing an import cycle.
    from packages.tailor.targeted import taken_away

    gone = {normalize(word) for word in taken_away(original, candidate)}
    if not gone:
        return ()

    facts = _items(original)
    # A lowercase identifier can name a library too. Require its explicit
    # label, rather than guessing that every unfamiliar prose word is a tool.
    facts.extend(
        match.group(1)
        for match in _LABELLED_NAME.finditer(original)
        if normalize(match.group(1)) not in _COMMON_WORDS
    )
    for match in _TOKEN_RE.finditer(original):
        raw = match.group().strip(".,;:")
        word = normalize(raw)
        named = (
            word not in _COMMON_WORDS
            and any(char.isupper() for char in raw)
            and (match.start() > 0 or any(char.isupper() for char in raw[1:]))
        )
        if named or _COMPOUND.fullmatch(raw):
            facts.append(raw)

    lost: list[str] = []
    for fact in facts:
        if not any(normalize(word) in gone for word in taken_away(fact, candidate)):
            continue
        # A space in place of a hyphen is a spelling change, not a deletion.
        if not taken_away(fact.replace("-", " "), candidate.replace("-", " ")):
            continue
        if fact not in lost:
            lost.append(fact)
    return tuple(lost)
