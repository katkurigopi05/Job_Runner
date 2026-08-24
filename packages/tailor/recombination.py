"""Fabrication by recombination: true words arranged into an untrue claim.

`guard.check` verifies tokens. Every claim-carrying token in a rewritten
bullet must trace to the source, and that catches the obvious invention — a
framework the owner never used, an employer they never had.

It cannot catch recombination, and not by oversight: a token-level check has
no representation of which words stood *next to* which. If a résumé says
"Kubernetes" under one employer and "cluster administration" under another,
then "Kubernetes cluster administration" passes with every token supported,
and the sentence asserts something the résumé never did. §2.1 asks that every
noun-phrase entity trace to the source; token checking answers a weaker
question and reports it as if it were that one.

## Why adjacency and not noun-phrase chunking

Chunking noun phrases properly needs a part-of-speech tagger, a tagger needs a
tagged corpus, and every route to one — nltk's `averaged_perceptron_tagger`,
spaCy's models — downloads at first use. §3 puts the whole stack on a machine
that works offline, and `embed.py` already defends that boundary by defaulting
to a backend with no download. Buying phrase awareness with a runtime model
fetch is the wrong trade for this project.

Adjacency needs none of it. Two claim-carrying tokens standing next to each
other in the output are checked for standing near each other somewhere in the
source. It is a weaker instrument than a real chunker and catches the case
that matters, which is words moving between contexts.

## It does not reject

`GuardReport.ok` is unchanged by anything here. Findings are recorded so the
false-positive rate can be measured against real rewrites first — a
legitimate rephrasing reorders words, and reordering is exactly what this
notices. Promoting it to a rejection before that is known would raise the
guard's rejection rate for no measured gain, which docs/REFERENCE.md §3.6
names as the trap: tuning against the one referee we control.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from packages.tailor.guard import (
    _COMMON_WORDS,
    CorpusItem,
    SourceCorpus,
    normalize,
    singular,
)

#: How far apart two tokens may stand *within one line or sentence* and still
#: count as having appeared together. Six spans a clause.
#:
#: The segmenting is what does the real work. A flat token window over the
#: whole résumé silently spans entries: in the test fixture "Kubernetes" and
#: "cluster" sit exactly six tokens apart across a blank line and two
#: different employers, so a flat window vouched for precisely the pair this
#: module exists to catch. Distance is not adjacency when a line break sits
#: between.
WINDOW = 6

_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#.\-/]*")

#: Line breaks and sentence enders. Both bound a claim.
_SEGMENT_RE = re.compile(r"[\n\r]+|(?<=[.!?;])\s+")


@dataclass(frozen=True)
class Term:
    """A content word in the output.

    Not `guard.Entity`. `_classify` recognises only proper nouns, numbers,
    acronyms and scope verbs, because those are the tokens that carry a claim
    *on their own*. Recombination is the opposite case: ordinary words —
    "cluster", "administration" — that assert nothing alone and a great deal
    in company. Filtering by `_classify` here found nothing at all, which is
    how this distinction got made.
    """

    text: str
    normalized: str


@dataclass(frozen=True)
class Recombination:
    """Two supported terms adjacent in the output but never in the source."""

    first: Term
    second: Term

    def __str__(self) -> str:
        return (
            f"{self.first.text!r} and {self.second.text!r} are each supported "
            f"but never appear together in the source"
        )


def _segments(text: str) -> list[list[str]]:
    """Normalized tokens in order, grouped by line and sentence.

    Segmented rather than flat because a résumé is a list of separate claims.
    Two words in different entries are not "near" each other in any sense that
    licenses putting them in one phrase, however few tokens happen to lie
    between them.
    """
    segments: list[list[str]] = []
    for chunk in _SEGMENT_RE.split(text):
        tokens = [
            normalized for token in _WORD_RE.findall(chunk) if (normalized := normalize(token))
        ]
        if tokens:
            segments.append(tokens)
    return segments


def _co_occurs(segments: list[list[str]], first: str, second: str, *, window: int = WINDOW) -> bool:
    for tokens in segments:
        here = [index for index, token in enumerate(tokens) if token == first]
        there = [index for index, token in enumerate(tokens) if token == second]
        if any(abs(a - b) <= window for a in here for b in there):
            return True
    return False


def _supported(normalized: str, available: frozenset[str] | set[str]) -> bool:
    return normalized in available or singular(normalized) in available


def find(
    output: str,
    corpus: SourceCorpus,
    *,
    scope: CorpusItem | None = None,
    window: int = WINDOW,
) -> list[Recombination]:
    """Adjacent claim-carrying pairs in `output` that never stand together in
    the source.

    Only pairs whose members are *both* individually supported are reported.
    An unsupported token is already a violation, and reporting it twice under
    two names would make the guard's output harder to read, not safer.
    """
    source_text = scope.text if scope is not None else corpus.text
    source = _segments(source_text)
    if not source:
        return []

    available = scope.tokens if scope is not None else corpus.tokens
    findings: list[Recombination] = []
    seen: set[tuple[str, str]] = set()

    previous: Term | None = None
    for token in _WORD_RE.findall(output):
        normalized = normalize(token)
        if not normalized or len(normalized) < 3 or normalized in _COMMON_WORDS:
            # An ordinary word breaks adjacency. "Kubernetes and cooking" is
            # not the assertion "Kubernetes cooking".
            previous = None
            continue

        term = Term(text=token, normalized=normalized)
        if previous is not None and previous.normalized != term.normalized:
            pair = (previous.normalized, term.normalized)
            if (
                pair not in seen
                # Both individually supported: an unsupported token is already
                # a guard violation, and naming it twice makes the output
                # harder to read rather than safer.
                and _supported(previous.normalized, available)
                and _supported(term.normalized, available)
                and not _co_occurs(source, *pair, window=window)
            ):
                seen.add(pair)
                findings.append(Recombination(first=previous, second=term))
        previous = term

    return findings
