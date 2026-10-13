"""A level named in a question, and where a title stands against it.

"entry level software engineer jobs" showed three senior postings on the
owner's database, "junior backend roles" five, and "internships in data
science" nothing while 196 intern titles were open. A level was searched for
as a word in descriptions, where "entry" and "junior" turn up in senior
postings ("mentor junior engineers"), and "internships" is in no title:
titles say "Intern".

Two halves, deliberately different:

- **A title's level is the feed's** (`filters.detect_seniority`). One ladder,
  so the assistant and the feed cannot read "Staff" two ways.
- **A question's level is a shorter list.** The ladder also knows
  "architect", "director", "lead" and "associate", and in a title those are
  levels. In a question they are the job: "solutions architect jobs" asks for
  architects, and reading it as "principal-level jobs about solutions" would
  answer something else. So only the words that are a level and nothing else
  are read here.

What a title that names *no* level is, is not decided here. Half the owner's
corpus is such titles, and `retrieve` is told by a setting whether to leave
them out or list them after (`CHAT_ALLOW_UNKNOWN_SENIORITY`). A title naming
another level is never at the level asked.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from packages.matching.embed import tokenize
from packages.matching.filters import detect_seniority

#: The ladder's rungs, lowest first, with the ways a *question* names each.
_ASKED: tuple[tuple[str, str], ...] = (
    ("intern", r"interns?|internships?|co-?ops?"),
    (
        "junior",
        # "entry" on its own is not one ("data entry jobs"); "entry or mid
        # level" is, and names both rungs.
        r"junior|jr|entry[- ]level|entry(?=[\s-]*(?:or|and|to|/)\s*mid)"
        r"|new[- ]grad(?:uate)?s?|early[- ]career|graduate",
    ),
    ("mid", r"mid[- ]level"),
    ("senior", r"senior|sr|staff"),
    ("principal", r"principal|distinguished"),
)
_PATTERNS = tuple(
    (level, re.compile(rf"(?<![a-z0-9])(?:{words})(?![a-z0-9])", re.IGNORECASE))
    for level, words in _ASKED
)
#: "not senior" and "non-senior" ask for the other levels. That question is
#: left to the search as it was, like the opposite one about sponsorship.
_NEGATION = re.compile(
    r"(?:\b(?:not|no|except|excluding|without|other\s+than)\s+(?:a\s+|an\s+|the\s+)?|\bnon[- ]?)$",
    re.IGNORECASE,
)
#: The ladder's other words. In a title they are a level, and in a question
#: they are the job. A question that says one is naming a title ("senior
#: director", "senior solutions architect"), and the title search answers it:
#: read as a level, "senior director" was held to the senior rung, and a
#: "Senior Director" is on the principal one, because "director" is.
_TITLE_NOUNS = re.compile(
    r"(?<![a-z0-9])(?:architects?|directors?|vps?|vice[- ]presidents?|head of|leads?"
    r"|associates?|apprentice\w*|trainees?)(?![a-z0-9])",
    re.IGNORECASE,
)
#: The word a rung's titles are likely to say for each way of asking. "senior"
#: and "sr" are one word; "staff" is another on the same rung. A way of asking
#: with no word of its own in titles ("entry level") has none.
_SAME_WORD: tuple[tuple[str, str], ...] = (
    (r"senior|sr", r"senior|sr"),
    (r"junior|jr", r"junior|jr"),
    (r"intern\w*|co-?ops?", r"intern\w*|co-?ops?"),
    (r"\w*grad\w*", r"\w*grad\w*"),
    (r"staff", r"staff"),
    (r"principal", r"principal"),
    (r"distinguished", r"distinguished"),
)

#: An internship is a posting whatever else the question says: "internships in
#: data science" has no word for a job in it and was not searched at all.
_POSTINGS_BY_NAME = frozenset({"intern"})


@dataclass(frozen=True)
class AskedLevel:
    #: Rungs of the ladder, lowest first.
    levels: tuple[str, ...]
    #: The words as typed, lower case: "entry level", "staff".
    said: tuple[str, ...]
    #: Their tokens. Not searched for: a level is read from the title.
    words: frozenset[str]

    @property
    def implies_postings(self) -> bool:
        return bool(_POSTINGS_BY_NAME & set(self.levels))

    @property
    def spoken(self) -> str:
        return " or ".join(self.said)


@dataclass(frozen=True)
class LevelCount:
    """How the postings found stand against the level asked, counted."""

    said: str
    at_level: int
    #: Titles that name no level at all.
    unknown: int
    #: Titles that name a different one. Never listed.
    other: int
    #: Whether the ones naming no level were listed after the rest.
    unknown_listed: bool


def asked_level(question: str) -> AskedLevel | None:
    """The level or levels a question asks for, or None."""
    if _TITLE_NOUNS.search(question):
        return None
    levels: list[str] = []
    said: list[str] = []
    for level, pattern in _PATTERNS:
        for match in pattern.finditer(question):
            if _NEGATION.search(question[: match.start()]):
                return None
            if level not in levels:
                levels.append(level)
            said.append(" ".join(match.group(0).lower().split()))
    if not levels:
        return None
    spoken = tuple(dict.fromkeys(said))
    return AskedLevel(
        levels=tuple(levels),
        said=spoken,
        words=frozenset(token for phrase in spoken for token in tokenize(phrase)),
    )


def _typed_in(title: str, asked: AskedLevel) -> bool:
    """Whether the title says the word that was typed, in any of its spellings."""
    for phrase in asked.said:
        for typed, in_titles in _SAME_WORD:
            if re.fullmatch(typed, phrase, re.IGNORECASE):
                if re.search(rf"(?<![a-z0-9])(?:{in_titles})(?![a-z0-9])", title, re.IGNORECASE):
                    return True
                break
        else:
            # No word of its own in titles: every title on the rung says it as well.
            return True
    return False


def standing(title: str | None, asked: AskedLevel) -> int:
    """0 the title says the word typed, 1 it is on that rung, 2 it names no
    level, 3 it names another.

    0 and 1 are the same answer to "is it at the level", and are told apart
    only where nothing else orders the list: asked for staff, a role's
    postings lead with the ones that say staff. Where the search has an order
    of its own, `at_the_level` is the question to ask.
    """
    text = title or ""
    level = detect_seniority(text)
    if level is None:
        return 2
    if level not in asked.levels:
        return 3
    return 0 if _typed_in(text, asked) else 1


def at_the_level(title: str | None, asked: AskedLevel) -> bool:
    return standing(title, asked) < 2


def sift[T](
    hits: list[T], asked: AskedLevel, *, allow_unknown: bool, title: Callable[[T], str | None]
) -> tuple[list[T], LevelCount]:
    """`hits` at the level asked, in the order given, and the count of all of them.

    With `allow_unknown`, the ones whose title names no level are kept too.
    Their place after the others is the caller's to give (`standing`), once
    it has finished ordering.
    """
    places = [standing(title(hit), asked) for hit in hits]
    keep = (0, 1, 2) if allow_unknown else (0, 1)
    return (
        [hit for hit, place in zip(hits, places, strict=True) if place in keep],
        LevelCount(
            said=asked.spoken,
            at_level=sum(place < 2 for place in places),
            unknown=places.count(2),
            other=places.count(3),
            unknown_listed=allow_unknown,
        ),
    )


__all__ = ["AskedLevel", "LevelCount", "asked_level", "at_the_level", "sift", "standing"]
