"""What a job description asks for, and which of it the résumé already supports.

§2.1 permits injecting keywords "that are already supported by the source
résumé". Nothing computed that set — the whole description was handed to the
model with an instruction not to invent, and the instruction was the only
safeguard. Prompts are advisory; models disregard them.

This computes the intersection instead. The model is told which of the job's
terms the résumé genuinely backs, so the keywords it is invited to emphasize
are safe by construction rather than by request. Terms the résumé does not
support are named too, as the things it must not reach for.

That does not replace the guard. It narrows what the model is likely to do
wrong; the guard is what catches it when it does anyway.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

# Aliased: this module has its own `_TOKEN_RE` with a different pattern, and
# `borrowed_terms` has to tokenize the way the guard does to agree with it.
from packages.tailor.guard import _TOKEN_RE as _GUARD_TOKEN_RE
from packages.tailor.guard import SourceCorpus, normalize, singular, stem

#: Grammatical words and posting boilerplate.
#:
#: Deliberately *not* the guard's `_COMMON_WORDS`. That list answers "is this
#: token too ordinary to be evidence of fabrication", and it contains
#: `reliability`, `services`, `backend`, `engineer` — exactly the terms this
#: module exists to find. Reusing it reported a résumé that says "backend
#: services" and "reliability" as supporting none of a posting that asks for
#: all three. Two questions, two lists.
_STOPWORDS_TEXT = """
the a an and or but if then than that this these those with without within
for from into onto over under about across after before during while
you your our we us they them their his her its it is are was were be been
being have has had do does did will would shall should can could may might
must not no yes all any both each few more most other some such only own
same so too very just as at by in of on to up out off down
job role position candidate applicant company team work working experience
years year skills ability able strong excellent good great required requirement
requirements responsibilities responsible qualifications preferred plus nice
opportunity looking seeking join help build building including etc
improve improved improving increase increased increasing
scale scaled scaling manage managed managing lead led leading
support supported supporting create created creating
design designed designing develop developed developing
deliver delivered delivering
"""
# The verbs above arrived from PR #32. They are the ordinary vocabulary of any
# engineering posting, and leaving them in the "missing" set meant a rewrite
# that said "improved" was rejected for taking a term from the posting — the
# guard firing on grammar rather than on a claim.

_STOPWORDS: frozenset[str] = frozenset(_STOPWORDS_TEXT.split())

#: Words naming the role rather than the work.
#:
#: These are "supported" in the literal sense — the résumé says "Senior
#: Engineer" — so an earlier version offered them as terms to work in, and the
#: model stuffed the job title into the bullets: "Built backend services in
#: Python" became "Senior Backend Engineer in Python, built services". The
#: guard accepted it, because nothing was fabricated. It was still ruined.
#:
#: A bullet describes what was done. The title belongs in the header, and
#: matching on it is not evidence of fit either — every backend posting
#: contains the word "engineer".
_ROLE_WORDS_TEXT = """
senior sr junior jr staff principal lead leader director manager head chief
engineer engineering developer analyst architect scientist specialist
consultant intern internship apprentice apprenticeship trainee associate
officer president vice
"""

_ROLE_WORDS: frozenset[str] = frozenset(_ROLE_WORDS_TEXT.split())

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#./_-]{1,}")


@dataclass(frozen=True)
class TermReport:
    """The job's vocabulary, split by whether the résumé backs it."""

    #: Terms the résumé supports. Safe to emphasize — §2.1's "already supported".
    supported: list[str] = field(default_factory=list)
    #: Terms the job asks for that the résumé does not contain. Naming these is
    #: the point: they are exactly what a model reaches for when it invents.
    missing: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        """Share of the job's salient terms the résumé already covers.

        Not a match score — it counts vocabulary, not meaning. Useful as a
        blunt read on whether tailoring has anything to work with.
        """
        total = len(self.supported) + len(self.missing)
        return round(len(self.supported) / total, 3) if total else 0.0


def job_terms(job_description: str, *, limit: int = 40) -> list[str]:
    """Salient terms from a posting, most frequent first.

    Frequency rather than anything cleverer: a posting repeats what it cares
    about, and a term appearing once is as likely to be boilerplate as a
    requirement.

    Known multi-word skills are emitted whole. Tokenizing "machine learning"
    into "machine" and "learning" asks the wrong question twice: neither word
    is a skill, a résumé saying "ML" backs neither of them, and the pair went
    onto the off-limits list — so the model was forbidden from writing the one
    phrase the posting cared about most, for a skill the owner had. The words a
    phrase consumed are not also emitted alone, or the same skill appears twice
    with opposite verdicts.
    """
    phrases, consumed = _known_phrases(job_description)

    counts: Counter[str] = Counter()
    surface: dict[str, str] = {}

    for raw in _TOKEN_RE.findall(job_description):
        key = normalize(raw)
        if not key or len(key) < 3:
            continue
        if key in _STOPWORDS or key in _ROLE_WORDS:
            continue
        if key in consumed:
            continue
        counts[key] += 1
        # Keep the first spelling seen, so "PostgreSQL" is not shown as
        # "postgresql" to a human reading the diff.
        surface.setdefault(key, raw.strip(".,;:"))

    singles = [surface[key] for key, _ in counts.most_common(limit)]
    # Phrases first: a posting that names one is naming a skill, and the
    # single tokens below it are the long tail.
    return (phrases + singles)[:limit]


def _known_phrases(job_description: str) -> tuple[list[str], set[str]]:
    """Multi-word skills present in the posting, and the words they used up.

    Drawn from the alias table rather than a second list, because the phrases
    worth treating as one term are exactly the ones a résumé might write as an
    abbreviation — which is what that table already enumerates.
    """
    from packages.tailor.aliases import equivalents, known_phrases

    normalized = " ".join(normalize(raw) for raw in _TOKEN_RE.findall(job_description))
    found: list[str] = []
    consumed: set[str] = set()
    claimed: set[frozenset[str]] = set()
    for phrase in known_phrases():
        if phrase not in normalized:
            continue
        # One term per group. "data pipeline" and "data pipelines" are the same
        # skill written twice, and emitting both would ask the same question
        # twice and pad the list the model is handed.
        group = equivalents(phrase)
        if group in claimed:
            continue
        claimed.add(group)
        found.append(phrase)
        # Singular and plural both, or "data pipelines" consumes its own words
        # while "data pipeline tooling" elsewhere in the posting still leaks
        # "pipeline" out as a separate term — the same skill, asked twice.
        for word in phrase.split():
            consumed.add(word)
            consumed.add(singular(word))
    return found, consumed


def _supported(term: str, corpus: SourceCorpus) -> bool:
    """Whether the source material backs a term.

    Token match first, then a substring check on the normalized text — a
    multi-word term like "machine learning" is never a single token, and
    missing it would report the résumé as thinner than it is.

    Equivalent spellings count. A posting asking for "machine learning" is
    backed by a résumé that says "ML": the same claim, written shorter. Without
    this the term is reported unsupported, the model is forbidden from using
    the posting's own word for a skill the owner has, and the résumé loses the
    keyword an ATS is scanning for. `packages/tailor/aliases.py` holds the two
    rules that keep the table to true equivalences.
    """
    from packages.tailor.aliases import equivalents

    key = normalize(term)
    if not key:
        return False

    return any(form in corpus.tokens or form in corpus.text for form in equivalents(key) or {key})


def analyze(job_description: str, corpus: SourceCorpus, *, limit: int = 40) -> TermReport:
    """Split a posting's terms by whether the source material backs them."""
    supported: list[str] = []
    missing: list[str] = []

    for term in job_terms(job_description, limit=limit):
        if _supported(term, corpus):
            supported.append(term)
        else:
            missing.append(term)

    return TermReport(supported=supported, missing=missing)


def borrowed_terms(original: str, candidate: str, forbidden: tuple[str, ...]) -> list[str]:
    """Posting terms the rewrite introduced that the source did not have.

    The complement of `analyze`: that names the terms the résumé cannot back,
    this catches the model reaching for one anyway. A term counts as borrowed
    only when every word of it is present now and was not present before —
    a partial overlap is ordinary vocabulary, not a claim.

    Lives here rather than in `rewrite.py` because both the bullet rewriter
    and the cover letter ask the same question of their output.
    """
    if not forbidden:
        return []

    had = {stem(word) for word in _GUARD_TOKEN_RE.findall(original)}
    has = {stem(word) for word in _GUARD_TOKEN_RE.findall(candidate)}

    borrowed = []
    for term in forbidden:
        term_stems = {stem(word) for word in _GUARD_TOKEN_RE.findall(term)}
        if term_stems and term_stems <= has and not term_stems <= had:
            borrowed.append(term)
    return borrowed
