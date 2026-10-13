"""Which question is asking for postings that sponsor, and what a posting says.

The half of `retrieve._stating_sponsorship` that needs nothing from the
search: recognising the question, the words in it that must not be searched
for, and a posting's verdict.

`eligibility.read_posting` is the reader. It has told the feed what a posting
states about sponsorship since §15, and nothing asked it on behalf of the
assistant: "jobs that sponsor H-1B" was a search for those words, and the
postings that say them most are the ones saying no. On the owner's database
on 2026-10-13, in the search area, 334 postings stated they do not sponsor
and 15 that they do.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from packages.matching.eligibility import Sponsorship, read_posting
from packages.matching.embed import tokenize

#: A posting that says none of these has said nothing about sponsorship, so
#: it is not read: `read_posting` scans every sentence, and a role can be a
#: thousand postings. Also the test that finds the ones to read when a
#: question names nothing else. A Postgres pattern and a Python one.
MENTIONS = r"sponsor|visa|h-?1b"
_MENTIONS = re.compile(MENTIONS, re.IGNORECASE)

_VISA = r"visas?|h-?1b"
_VERB = r"sponsor(?:s|ed|ing)?"
#: "sponsorship" is a visa question after a word that offers it. On its own it
#: is also a job: "Sponsorship Sales Manager jobs" asks for those.
_OFFERED = (
    r"(?:offer\w*|provid\w*|giv\w*|with|have|has|having|get|gets|includ\w*|availab\w*)"
    r"(?:\s+\w+){0,2}\s+sponsorships?"
)
_ASKS = re.compile(rf"\b(?:{_VISA}|{_VERB}|{_OFFERED})\b", re.IGNORECASE)
_WORDS = re.compile(rf"\b(?:{_VISA}|sponsor\w*)\b", re.IGNORECASE)
#: The opposite question is a different one, and is left to the search as it
#: was: "which jobs do not sponsor" is not answered by the ones that do.
_NEGATED = re.compile(
    r"\b(?:no|not|never|without|don'?t|doesn'?t|won'?t|can'?t|cannot|unable|isn'?t|aren'?t)\b",
    re.IGNORECASE,
)

#: Words that ask for it without naming a thing to look for. "offer" is in
#: most postings ("we offer"), and found alone it would find them.
_ASKING = frozenset(
    [
        "offer",
        "offers",
        "offering",
        "offered",
        "provide",
        "provides",
        "providing",
        "give",
        "gives",
        "available",
        "h",
        "1b",
        "h1b",
        "b",
    ]
)


@dataclass(frozen=True)
class SponsorshipCount:
    """What the postings a question was about say, counted. Never a guess:
    `unstated` is not folded into either of the other two (§16, §18)."""

    offered: int
    refused: int
    #: Said nothing, or said something that cannot be resolved either way.
    unstated: int
    #: What was counted over, in words: "data engineer postings in the search
    #: area", or the postings that mention sponsorship at all.
    over: str
    #: True when `over` is only the postings that mention it, so `unstated`
    #: is the unresolved ones and every other posting is silent.
    mentions_only: bool = False


def asks_which_sponsor(question: str) -> bool:
    """Whether the question asks for postings that offer sponsorship."""
    return bool(_ASKS.search(question)) and not _NEGATED.search(question)


def asking_words(question: str) -> frozenset[str]:
    """The question's words that ask for it. They are not searched for: the
    postings that say "sponsor" and "H-1B" most are refusing."""
    said = {token for match in _WORDS.finditer(question) for token in tokenize(match.group(0))}
    return frozenset(said) | _ASKING


def verdict(description: str | None) -> Sponsorship:
    """What a posting states. Unstated without reading one that never mentions it."""
    if not description or not _MENTIONS.search(description):
        return Sponsorship.UNSTATED
    return read_posting(description).sponsorship


def sentence_that_says_so(description: str | None) -> str:
    """The posting's own sentence about sponsorship, for the excerpt."""
    evidence = read_posting(description).evidence
    about = [item.quote for item in evidence if _MENTIONS.search(item.quote)]
    return (about or [item.quote for item in evidence] or [""])[0]


__all__ = [
    "MENTIONS",
    "SponsorshipCount",
    "asking_words",
    "asks_which_sponsor",
    "sentence_that_says_so",
    "verdict",
]
