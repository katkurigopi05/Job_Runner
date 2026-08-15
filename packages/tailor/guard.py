"""The fabrication guard — CLAUDE.md §2.1, and the merge gate for tailoring.

Rewriting may rephrase, reorder, re-emphasize, and inject keywords *already
supported by the source*. It may not add a skill, employer, date, credential,
or metric that is not there. This module is what makes that checkable rather
than aspirational.

## How it decides

Extract from the candidate output every token that could carry a factual
claim, then require each to trace back to the source corpus:

- **Numbers** — the metric case. "improved latency by 40%" is a fabrication
  unless 40 appears in the source.
- **Proper nouns** — employers, products, technologies. A capitalized token
  that is not ordinary English has to come from somewhere.
- **Acronyms** — skills and credentials. AWS, PMP, CISSP.
- **Years** — the date case.

Ordinary English words, however rearranged, are free. That is exactly the
latitude §2.1 grants: rephrasing is allowed, inventing is not.

## Why it is deliberately strict

A guard that misses a fabrication puts a false claim on a real application in
the owner's name. A guard that is over-strict merely refuses a rewrite and
falls back to the source résumé, which is always safe. The asymmetry is the
whole design, and it is why the checks below reject on doubt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

# A number keeps its unit: 40TB, 800ms and 2M are single claims, not a
# number sitting next to an unrelated acronym.
_TOKEN_RE = re.compile(r"\d[\d,.]*%?[A-Za-z]*|[A-Za-z][A-Za-z0-9+#./-]*")
_NUMERIC_RE = re.compile(r"\d")
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")

#: Written numbers a rewrite may legitimately render as digits, and vice
#: versa. "three years" → "3 years" restates a source fact rather than
#: inventing one.
_NUMBER_WORDS: dict[str, str] = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
    "fifteen": "15",
    "twenty": "20",
    "thirty": "30",
    "forty": "40",
    "fifty": "50",
    "hundred": "100",
    "thousand": "1000",
    "million": "1000000",
}

#: Ordinary English. A capitalized token from this set is sentence casing,
#: not a proper noun, so it never needs to trace to the source.
#:
#: Kept as text rather than an inline literal so the formatter leaves it
#: readable — a wall of quoted strings is a list nobody will maintain.
_COMMON_WORDS_TEXT = """
a ability about above achieved across after again against all almost also
although always among an and any api apis application applications approach
architected architecture are as at automation backend be because been before
being below between both build built but by changes clients code
collaborated component components created cut data database databases day
decreased delivered deployment design designed developed development drove
during each either else enabled end engineer engineering engineers enough
ensured even ever every expanded experience feature features few first
following for from frontend further growth had has have he her here his how
however i if impact implemented improved in increased infrastructure
integration into introduced is it its just last latency later launched least
led less level library like logic maintained managed many may member
mentored metrics migrated migration model models module more most much must
never new next no not now number of often on once only opportunity optimized
or other our over own owned part partner partnered people per performance
performed pipeline pipelines planned platform practice process processes
produced product production project projects quality queries query rather
reduced refactored release released reliability removed replaced
requirements research response result results review reviews role same scale
scaled scope senior service services several she shipped simplified since so
software solution solutions solved some speed staff standards strategy
streamlined structure such support supported system systems team teams
technology test testing tests than that the their them then there these they
this through throughout thus time to today too tool tooling tools traffic
type under until up upon use used user users using value version very via
was week well were what when where whether which while who will with within
without work worked workflow working works would wrote year years yet you
your
"""

_COMMON_WORDS: frozenset[str] = frozenset(_COMMON_WORDS_TEXT.split())


class EntityKind(StrEnum):
    NUMBER = "number"
    PROPER_NOUN = "proper_noun"
    ACRONYM = "acronym"
    YEAR = "year"


@dataclass(frozen=True)
class Entity:
    """A token in the output that carries a factual claim."""

    text: str
    kind: EntityKind
    normalized: str


@dataclass(frozen=True)
class Violation:
    """An entity that does not trace back to the source."""

    entity: Entity
    reason: str

    def __str__(self) -> str:
        return f"{self.entity.kind.value} {self.entity.text!r}: {self.reason}"


@dataclass
class GuardReport:
    """The verdict on one piece of candidate output."""

    ok: bool
    violations: list[Violation] = field(default_factory=list)
    checked: int = 0

    def summary(self) -> str:
        if self.ok:
            return f"clean — {self.checked} entities traced to source"
        listed = "; ".join(str(v) for v in self.violations[:5])
        more = "" if len(self.violations) <= 5 else f" (+{len(self.violations) - 5} more)"
        return f"{len(self.violations)} unsupported: {listed}{more}"


class FabricationError(Exception):
    """Candidate output contains claims the source does not support."""

    def __init__(self, report: GuardReport) -> None:
        self.report = report
        super().__init__(report.summary())


def _strip(token: str) -> str:
    return token.strip(".,;:!?()[]{}\"'")


def normalize(token: str) -> str:
    """Fold a token to the form the corpus is indexed under.

    Lowercase, punctuation-stripped, naive de-pluralized, number-words mapped
    to digits, and thousands separators removed so `2,000` matches `2000`.
    """
    text = _strip(token).lower()
    if not text:
        return ""

    if text in _NUMBER_WORDS:
        return _NUMBER_WORDS[text]

    # 2,000 -> 2000 ; 40% -> 40 ; 2m -> 2m (the unit carries meaning)
    if _NUMERIC_RE.search(text):
        text = text.replace(",", "").rstrip("%+")

    return text


def singular(text: str) -> str:
    """Naive de-pluralization, used only for *matching*.

    Applied as an alternate index rather than folded into `normalize`, so
    "Kubernetes" is never reported back to the owner as "kubernete".
    """
    if len(text) > 3 and text.endswith("s") and not text.endswith(("ss", "us", "is")):
        return text[:-1]
    return text


def _classify(token: str) -> EntityKind | None:
    """What kind of claim this token carries, or None if it carries none."""
    bare = _strip(token)
    if not bare:
        return None

    if _YEAR_RE.match(bare):
        return EntityKind.YEAR

    if _NUMERIC_RE.search(bare):
        # A bare ordinal or list marker is not a metric.
        return EntityKind.NUMBER

    if len(bare) >= 2 and bare.isupper() and bare.isalpha():
        return EntityKind.ACRONYM

    if bare[0].isupper() and normalize(bare) not in _COMMON_WORDS:
        return EntityKind.PROPER_NOUN

    return None


def extract_entities(text: str) -> list[Entity]:
    """Every claim-carrying token in `text`, in order, deduplicated."""
    seen: set[tuple[str, EntityKind]] = set()
    entities: list[Entity] = []

    for match in _TOKEN_RE.finditer(text):
        token = match.group(0)
        kind = _classify(token)
        if kind is None:
            continue
        normalized = normalize(token)
        if not normalized:
            continue
        key = (normalized, kind)
        if key in seen:
            continue
        seen.add(key)
        entities.append(Entity(text=_strip(token), kind=kind, normalized=normalized))

    return entities


@dataclass
class SourceCorpus:
    """Everything a rewrite is allowed to draw on.

    The source résumé plus any other *verified* material — GitHub projects are
    included because they come from the owner's own account rather than from a
    model. Widening this set is how a fabrication becomes permissible, so it
    should be done deliberately and never quietly.
    """

    tokens: set[str] = field(default_factory=set)
    #: Normalized full text, for substring checks a token set would miss.
    text: str = ""

    @classmethod
    def from_texts(cls, *texts: str) -> SourceCorpus:
        tokens: set[str] = set()
        joined: list[str] = []
        for raw in texts:
            if not raw:
                continue
            joined.append(raw.lower())
            for match in _TOKEN_RE.finditer(raw):
                normalized = normalize(match.group(0))
                if normalized:
                    tokens.add(normalized)
                    tokens.add(singular(normalized))
                    # Index the digit form of a written number too, so a source
                    # saying "three" supports an output saying "3".
                    if normalized in _NUMBER_WORDS:
                        tokens.add(_NUMBER_WORDS[normalized])
        return cls(tokens=tokens, text="\n".join(joined))

    def supports(self, entity: Entity) -> bool:
        if entity.normalized in self.tokens or singular(entity.normalized) in self.tokens:
            return True
        # A hyphenated or slashed compound is supported when each part is.
        parts = [p for p in re.split(r"[-/]", entity.normalized) if p]
        return len(parts) > 1 and all(part in self.tokens for part in parts)


def check(output: str, corpus: SourceCorpus) -> GuardReport:
    """Verify every claim in `output` traces to `corpus`."""
    entities = extract_entities(output)
    violations = [
        Violation(entity=entity, reason="does not appear in the source material")
        for entity in entities
        if not corpus.supports(entity)
    ]
    return GuardReport(ok=not violations, violations=violations, checked=len(entities))


def check_or_raise(output: str, corpus: SourceCorpus) -> GuardReport:
    """Same, but refuse to return output that fabricates."""
    report = check(output, corpus)
    if not report.ok:
        raise FabricationError(report)
    return report
