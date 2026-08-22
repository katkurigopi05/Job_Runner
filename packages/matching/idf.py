"""Inverse document frequency over the postings we already hold.

Several modules carry a hand-written list of words that "appear in every job
posting and describe nothing" — `score._BOILERPLATE`, `keywords._STOPWORDS`.
Both are me guessing at a distribution. IDF measures it: a term in most
postings is boilerplate by definition, and one in few is distinguishing.

Three things that buys, in order of how much they matter:

**It removes a calibration nobody could defend.** `legitimacy.MIN_SPECIFICITY`
was set from two fixtures written in this repo, which is the circularity in
docs/REFERENCE.md §3.6 — a threshold tuned to its own samples measures the
samples. A corpus statistic has no such problem.

**It adapts to what the owner actually searches.** Someone crawling only ML
companies sees "machine learning" in every posting, so it distinguishes
nothing and should stop being reported as a gap. A fixed list cannot know
that; a measured one does automatically.

**It stops the lists drifting apart.** Four places currently answer "is this
word interesting" with four different answers.

## Why this is not applied to stored vectors

The obvious next step is weighting `LexicalEmbedder` by IDF, and it is a trap
in the same shape as the embedder-mixing bug: IDF moves as the corpus grows,
so a posting embedded at 50 documents sits in a different space from one
embedded at 5,000, and cosine between them degrades with no error anywhere.
Doing that safely needs the vectors recomputed whenever the statistics move,
which is real machinery.

Everything here runs on the report paths instead — `missing_terms`,
`specificity` — which are computed fresh on every scoring pass and never
stored. Those cannot drift.

## Small corpora

Below `MIN_DOCUMENTS` the statistic is noise: with 20 postings, a term in
three of them looks rare because the sample is small, not because it is.
`from_texts` still builds, but `usable` says no and callers fall back to the
hand-written list, which is at least a considered guess.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from packages.matching.embed import tokenize

#: Under this many documents, IDF describes the sample rather than the domain.
MIN_DOCUMENTS = 50

#: A term in at least this share of documents says nothing about any one of
#: them. Used as the boilerplate cut where a hard boundary is wanted.
BOILERPLATE_DOCUMENT_SHARE = 0.6

#: Below this share, a term separates this posting from most others, so one
#: mention is enough to report it. Without a corpus the caller has to guess at
#: this with repetition and capitalization; with one it is simply known.
DISTINGUISHING_DOCUMENT_SHARE = 0.3


@dataclass(frozen=True)
class DocumentFrequencies:
    """How many documents each term appeared in, and how many there were."""

    total: int = 0
    counts: Mapping[str, int] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.total >= MIN_DOCUMENTS

    @classmethod
    def from_texts(cls, texts: Iterable[str]) -> DocumentFrequencies:
        """Count documents per term. A term repeated within one counts once."""
        counts: Counter[str] = Counter()
        total = 0
        for text in texts:
            if not text or not text.strip():
                continue
            total += 1
            counts.update(set(tokenize(text)))
        return cls(total=total, counts=dict(counts))

    def document_share(self, term: str) -> float:
        """Fraction of documents containing `term`. 0.0 when unseen."""
        if not self.total:
            return 0.0
        return self.counts.get(term, 0) / self.total

    def idf(self, term: str) -> float:
        """Smoothed IDF. Higher means the term distinguishes more.

        `log((N + 1) / (df + 1)) + 1`, the standard smoothing: an unseen term
        gets the highest weight rather than dividing by zero, and every weight
        stays positive so a common term is never *negative* evidence.
        """
        if not self.total:
            return 1.0
        return math.log((self.total + 1) / (self.counts.get(term, 0) + 1)) + 1.0

    def is_boilerplate(self, term: str) -> bool:
        """Whether this term is too widespread to say anything."""
        return self.document_share(term) >= BOILERPLATE_DOCUMENT_SHARE

    def is_distinguishing(self, term: str) -> bool:
        """Whether the term sets this posting apart from most of the corpus.

        A term seen in few documents is worth reporting on a single mention.
        The repetition and capitalization heuristics elsewhere are proxies for
        this question; here it is answered directly.
        """
        return self.document_share(term) <= DISTINGUISHING_DOCUMENT_SHARE

    def weigh(self, term: str, occurrences: int) -> float:
        """tf-idf for one term in one document, with sublinear tf.

        The tenth "Python" says little the first did not — the same scaling
        `LexicalEmbedder` uses, so the two agree about term weight.
        """
        if occurrences <= 0:
            return 0.0
        return (1.0 + math.log(occurrences)) * self.idf(term)
