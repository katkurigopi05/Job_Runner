"""What a posting actually says about work authorization, and what it does not.

Split out of `filters.py` because one regex was answering three different
questions and getting two of them wrong. Measured against the current code
before this existed:

    "We do not offer visa sponsorship."                 -> kept  (should exclude)
    "We cannot sponsor now or in the future."            -> kept  (should exclude)
    "Candidates with or without sponsorship ... apply."  -> EXCLUDED (should keep)
    "US citizens only."                                  -> kept, unflagged

The first two are false negatives from a pattern list that never learned
negation: it matched `no visa sponsorship` and `unable to sponsor` literally,
so the commonest English phrasing — a negated verb, `do not offer` — sailed
through. The third is the opposite error and the expensive one: `without
sponsorship` is a substring of `with or without sponsorship`, which is a
posting going out of its way to say sponsorship is *not* a barrier.

Three rules shape this module.

**Silence is not an answer.** §2.2 makes work-authorization answers verbatim
because they have legal consequences; the same caution applies to reading them.
A posting that says nothing about sponsorship has said nothing — it is not
"sponsorship available" and it is not "no sponsorship". It is `UNSTATED`, and
the screen says so.

**Ambiguity is not an answer either.** `with or without sponsorship` and
`regardless of sponsorship requirements` are real phrasings that do not resolve
to either verdict, and a posting that both refuses and offers sponsorship has
contradicted itself. All of those are `AMBIGUOUS`, which never excludes.

**Citizenship is a different fact from sponsorship.** "US citizens only" is not
a statement about sponsorship at all — it is a restriction a permanent resident
also fails, and one that sponsorship cannot fix. It gets its own verdict, its
own profile field, and its own exclusion reason.

Nothing here infers OPT or STEM-OPT acceptance, E-Verify participation, or a
history of H-1B sponsorship. None of those follow from the absence of a
restriction, and a feed that labelled a job "OPT-friendly" because the posting
happened not to mention visas would be inventing the one fact the applicant
cannot afford to be wrong about.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

#: Longest evidence sentence kept. Enough to read, short enough that an API
#: response and a dashboard card do not become a second copy of the posting.
MAX_EVIDENCE_CHARS = 240

#: What the owner should see when nothing can be concluded. Deliberately not
#: "no restrictions found", which reads as a clearance the posting never gave.
UNKNOWN_LABEL = "Unknown — verify with employer"


class Sponsorship(StrEnum):
    """What the posting says about sponsoring a visa."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    #: Said something, but not something that resolves — "with or without
    #: sponsorship", or a posting that states both.
    AMBIGUOUS = "ambiguous"
    UNSTATED = "unstated"


class Citizenship(StrEnum):
    """Whose citizenship or residency the posting restricts itself to."""

    CITIZENS_ONLY = "citizens_only"
    CITIZENS_OR_RESIDENTS_ONLY = "citizens_or_residents_only"
    UNSTATED = "unstated"


#: Sentence-splitter. Deliberately crude: postings are HTML-stripped prose and
#: bullet lists, and the unit this needs is "a clause whose negation scope is
#: its own". Splitting on sentence punctuation, newlines and bullet glyphs gets
#: that; a real parser would buy nothing here.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+|[\n\r]+|\s*[•·▪]\s*")

#: Abbreviations whose full stops are not sentence ends. Exactly the ones that
#: matter here, found by the splitter cutting `Must be a U.S. Citizen or Green
#: Card holder.` into `Must be a U.S.` and `Citizen or Green Card holder.` — so
#: the citizens-and-residents rule, which needs both halves in one clause, read
#: it as a bare citizens-only restriction and would have excluded a green-card
#: holder from a job explicitly open to one.
_PROTECTED_ABBREVIATIONS = re.compile(r"\bU\.\s?S\.\s?A\.|\bU\.\s?S\.", re.I)

#: Stands in for a full stop while splitting. A control character because no
#: posting contains one, and **the same length as what it replaces** so the
#: protected text can be restored piece by piece without tracking offsets.
_DOT = "\x01"

_MENTIONS_SPONSORSHIP = re.compile(r"sponsor(?:ship|ing|s|ed)?\b", re.I)

#: Checked **first**, because every phrase here contains a substring that the
#: negative list would otherwise claim. `with or without sponsorship` is the
#: one that was actually costing matches.
_AMBIGUOUS_SPONSORSHIP = re.compile(
    r"with\s+or\s+without\s+(?:the\s+)?(?:need\s+for\s+)?(?:visa\s+|employer\s+|employment\s+)?sponsor"
    r"|without\s+or\s+with\s+sponsor"
    r"|regardless\s+of\s+(?:your\s+)?(?:visa\s+|sponsorship|work\s+authorization)"
    r"|whether\s+or\s+not\s+you\s+(?:require|need)\s+sponsor"
    r"|either\s+with\s+or\s+without\s+sponsor",
    re.I,
)

#: A refusal. Two shapes, and the first is the one the old pattern could not
#: see: a negated verb or noun — `do not offer`, `cannot`, `is not able to`,
#: `not eligible for` — somewhere before `sponsor`. The gap is bounded so a
#: negation in a different clause of the same sentence does not reach across it.
_NO_SPONSORSHIP = re.compile(
    r"(?:"
    r"(?:do(?:es)?\s+not|cannot|can\s?not|will\s+not|won't|don't|doesn't|are\s+not|is\s+not|"
    r"unable|not\s+able|no\s+longer|not\s+eligible|ineligible|unwilling|not\s+offering|"
    r"not\s+in\s+a\s+position)"
    r"[^.;!?]{0,60}?sponsor"
    r"|no\s+(?:visa\s+|employer\s+|employment\s+|h-?1b\s+)?sponsorship"
    r"|sponsorship\s+(?:is\s+)?(?:not\s+available|unavailable|not\s+offered|not\s+provided)"
    r"|without\s+(?:the\s+need\s+for\s+)?(?:any\s+)?(?:current\s+or\s+future\s+)?"
    r"(?:visa\s+|employer\s+|employment\s+|company\s+)?sponsorship"
    r"|without\s+requiring\s+sponsorship"
    r")",
    re.I,
)

#: An offer. Narrower than the refusal list on purpose: a false "available" is
#: worse than a missed one, because the owner would read it as an answer.
_SPONSORSHIP_AVAILABLE = re.compile(
    r"(?:"
    r"sponsorship\s+(?:is\s+)?(?:available|offered|provided|possible)"
    r"|(?:we|company|employer)\s+(?:do\s+|can\s+|will\s+|are\s+able\s+to\s+|are\s+willing\s+to\s+)?"
    r"(?:offer|provide|support|sponsor)s?\s*(?:visa\s+|h-?1b\s+)?sponsor"
    r"|(?:offer|provide)s?\s+(?:visa\s+|employment\s+|employer\s+)?sponsorship"
    r"|(?:open\s+to|willing\s+to|able\s+to|happy\s+to)\s+sponsor"
    r"|will\s+sponsor"
    r"|visa\s+sponsorship\s+(?:available|offered|provided)"
    r")",
    re.I,
)

#: Citizens *and* residents. Checked before the citizens-only list, because
#: every phrasing here contains a citizens-only phrasing inside it, and the
#: difference decides whether a green-card holder is excluded.
_CITIZENS_OR_RESIDENTS = re.compile(
    r"(?:u\.?\s?s\.?|us|united\s+states)\s+"
    r"(?:citizens?(?:hip)?|persons?|nationals?)"
    r"[^.;!?]{0,60}?(?:permanent\s+resident|green\s+card|lawful\s+resident|lpr)"
    r"|(?:permanent\s+resident|green\s+card)[^.;!?]{0,60}?"
    r"(?:u\.?\s?s\.?|us|united\s+states)\s+citizens?",
    re.I,
)

_CITIZENS_ONLY = re.compile(
    r"(?:"
    r"(?:u\.?\s?s\.?|us|american|united\s+states)\s+citizens?(?:hip)?\s+"
    r"(?:only|required|is\s+required|mandatory)"
    r"|(?:only|restricted\s+to|limited\s+to|must\s+be|requires?)\s+"
    r"[^.;!?]{0,40}?(?:u\.?\s?s\.?|us|american|united\s+states)\s+citizens?"
    r"|must\s+be\s+a\s+(?:u\.?\s?s\.?|us|american|united\s+states)\s+citizen"
    r")",
    re.I,
)


@dataclass(frozen=True)
class Evidence:
    """One sentence from the posting, and what it was read as saying."""

    claim: str
    quote: str

    @staticmethod
    def of(claim: str, sentence: str) -> Evidence:
        quote = " ".join(sentence.split())
        if len(quote) > MAX_EVIDENCE_CHARS:
            quote = quote[: MAX_EVIDENCE_CHARS - 1].rstrip() + "…"
        return Evidence(claim=claim, quote=quote)


@dataclass(frozen=True)
class PostingEligibility:
    """What one posting states about authorization — and its own evidence.

    Deliberately about the **posting**, not about the owner. Whether a stated
    restriction excludes *this applicant* is a filter's job and depends on the
    profile; what the posting said is a property of the posting, so it can be
    shown on a card without knowing whose feed it is in.
    """

    sponsorship: Sponsorship = Sponsorship.UNSTATED
    citizenship: Citizenship = Citizenship.UNSTATED
    evidence: tuple[Evidence, ...] = field(default_factory=tuple)

    @property
    def certain(self) -> bool:
        """Whether the posting resolved either question."""
        return (
            self.sponsorship in (Sponsorship.AVAILABLE, Sponsorship.UNAVAILABLE)
            or self.citizenship is not Citizenship.UNSTATED
        )

    def restriction(self) -> str | None:
        """The citizenship restriction in words, or None if there is none.

        Separate from `summary` because the exclusion reason wants only this
        half: a reason reading "restricted to US citizens only; sponsorship:
        unknown — verify with employer" states two facts and excludes on one.
        """
        if self.citizenship is Citizenship.CITIZENS_ONLY:
            return "US citizens only"
        if self.citizenship is Citizenship.CITIZENS_OR_RESIDENTS_ONLY:
            return "US citizens or permanent residents only"
        return None

    def summary(self) -> str:
        """One line for a card. `UNKNOWN_LABEL` when nothing was established.

        The unknown case is phrased as an instruction rather than a finding on
        purpose: "not stated" invites the reader to assume it is fine, and the
        one thing this module must not do is let an absence read as permission.
        """
        parts: list[str] = []
        restricted = self.restriction()
        if restricted:
            parts.append(restricted)

        if self.sponsorship is Sponsorship.UNAVAILABLE:
            parts.append("states it does not sponsor")
        elif self.sponsorship is Sponsorship.AVAILABLE:
            parts.append("states sponsorship is available")
        elif self.sponsorship is Sponsorship.AMBIGUOUS:
            parts.append("mentions sponsorship without resolving it")

        if not parts:
            return UNKNOWN_LABEL
        if (
            self.sponsorship is Sponsorship.UNSTATED
            and self.citizenship is not Citizenship.UNSTATED
        ):
            parts.append(f"sponsorship: {UNKNOWN_LABEL.lower()}")
        return "; ".join(parts)

    def as_dict(self) -> dict[str, object]:
        """Wire form. Flat and explicit — a consumer should not have to infer."""
        return {
            "sponsorship": self.sponsorship.value,
            "citizenship": self.citizenship.value,
            "certain": self.certain,
            "summary": self.summary(),
            "evidence": [{"claim": e.claim, "quote": e.quote} for e in self.evidence],
        }


def _sentences(text: str) -> list[str]:
    """Clauses, with abbreviation full stops protected from the split.

    The protection is restored before anything is matched or quoted, so the
    patterns see — and the evidence shows — the posting's own wording.
    """
    protected = _PROTECTED_ABBREVIATIONS.sub(lambda m: m.group(0).replace(".", _DOT), text)
    return [
        part.replace(_DOT, ".")
        for part in _SENTENCE_SPLIT.split(protected)
        if part and part.strip()
    ]


def read_posting(text: str | None) -> PostingEligibility:
    """Read a posting's authorization statements. Never guesses.

    Precedence, and the reasoning for each:

    - **Ambiguous beats both.** A posting that says `with or without
      sponsorship` has told the reader sponsorship is not the deciding factor,
      and a posting that both refuses and offers has contradicted itself. In
      neither case may a filter exclude — the evidence does not support it.
    - **Refusal beats an offer** only when no ambiguity was found, because
      boilerplate offering "support for work authorization" alongside an
      explicit "this role cannot sponsor" is the refusal that governs.
    - **Citizens-and-residents is checked before citizens-only**, since every
      phrasing of the former contains the latter. Read the other way round, a
      posting open to green-card holders would exclude them.
    """
    if not text:
        return PostingEligibility()

    found: list[Evidence] = []
    refuses = offers = ambiguous = False
    citizenship = Citizenship.UNSTATED

    for sentence in _sentences(text):
        if _MENTIONS_SPONSORSHIP.search(sentence):
            if _AMBIGUOUS_SPONSORSHIP.search(sentence):
                ambiguous = True
                found.append(Evidence.of("sponsorship_ambiguous", sentence))
            elif _NO_SPONSORSHIP.search(sentence):
                refuses = True
                found.append(Evidence.of("sponsorship_unavailable", sentence))
            elif _SPONSORSHIP_AVAILABLE.search(sentence):
                offers = True
                found.append(Evidence.of("sponsorship_available", sentence))

        if citizenship is Citizenship.UNSTATED:
            if _CITIZENS_OR_RESIDENTS.search(sentence):
                citizenship = Citizenship.CITIZENS_OR_RESIDENTS_ONLY
                found.append(Evidence.of("citizens_or_residents_only", sentence))
            elif _CITIZENS_ONLY.search(sentence):
                citizenship = Citizenship.CITIZENS_ONLY
                found.append(Evidence.of("citizens_only", sentence))

    if ambiguous or (refuses and offers):
        sponsorship = Sponsorship.AMBIGUOUS
    elif refuses:
        sponsorship = Sponsorship.UNAVAILABLE
    elif offers:
        sponsorship = Sponsorship.AVAILABLE
    else:
        sponsorship = Sponsorship.UNSTATED

    return PostingEligibility(
        sponsorship=sponsorship, citizenship=citizenship, evidence=tuple(found)
    )


__all__ = [
    "MAX_EVIDENCE_CHARS",
    "UNKNOWN_LABEL",
    "Citizenship",
    "Evidence",
    "PostingEligibility",
    "Sponsorship",
    "read_posting",
]
