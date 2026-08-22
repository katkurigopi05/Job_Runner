"""Multinomial Naive Bayes over inbound message text.

Ported from the TA-GAI coursework implementation rather than taken from
scikit-learn, and that is a deliberate trade. sklearn would be three lines and
a black box; this is fifty lines that can name the tokens behind a verdict,
which is what makes it usable next to `RuleClassifier` — every classifier in
this package explains itself.

## Where it sits

`classify.py` runs rules first and a model when they abstain. This is the
middle tier: cheaper than the model, broader than the rules, and — unlike
either — it produces a *number*, so Gate 6's accuracy has something to be
compared against.

It is a bag of words. Order is discarded, so "we will not be moving forward"
and "we will be moving forward" differ only by the weight of "not". The rules
in `classify.py` exist precisely because that class of mistake is common in
recruiter mail, which is why this runs behind them rather than instead.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass

import structlog

from packages.core.enums import Classification
from packages.inbox.classify import ClassificationResult
from packages.matching.embed import tokenize

log = structlog.get_logger(__name__)

#: Add-one smoothing. Without it a single token missing from a class's
#: vocabulary sends that class's log-probability to -inf, and one unseen word
#: overrules an otherwise unambiguous message.
ALPHA = 1.0

#: How many tokens to name as evidence.
EVIDENCE_TERMS = 4


@dataclass(frozen=True)
class NaiveBayesClassifier:
    """A trained model. Immutable, so a fitted model cannot drift."""

    name = "bayes"

    #: log P(class)
    log_prior: dict[Classification, float]
    #: log P(token | class)
    log_likelihood: dict[Classification, dict[str, float]]
    #: The fallback for a token unseen in a class, per Laplace.
    log_unseen: dict[Classification, float]
    vocabulary: frozenset[str]

    @classmethod
    def train(
        cls, examples: list[tuple[Classification, str]], *, alpha: float = ALPHA
    ) -> NaiveBayesClassifier:
        """Fit from (label, text) pairs."""
        if not examples:
            raise ValueError("cannot train on an empty corpus")

        counts: dict[Classification, Counter[str]] = defaultdict(Counter)
        documents: Counter[Classification] = Counter()
        vocabulary: set[str] = set()

        for label, text in examples:
            tokens = tokenize(text)
            counts[label].update(tokens)
            documents[label] += 1
            vocabulary.update(tokens)

        total_documents = sum(documents.values())
        vocabulary_size = len(vocabulary)

        log_prior: dict[Classification, float] = {}
        log_likelihood: dict[Classification, dict[str, float]] = {}
        log_unseen: dict[Classification, float] = {}

        for label, token_counts in counts.items():
            log_prior[label] = math.log(documents[label] / total_documents)
            denominator = sum(token_counts.values()) + alpha * vocabulary_size
            log_likelihood[label] = {
                token: math.log((count + alpha) / denominator)
                for token, count in token_counts.items()
            }
            log_unseen[label] = math.log(alpha / denominator)

        return cls(
            log_prior=log_prior,
            log_likelihood=log_likelihood,
            log_unseen=log_unseen,
            vocabulary=frozenset(vocabulary),
        )

    def classify(self, subject: str, body: str) -> ClassificationResult:
        """Score every class and take the best.

        Tokens outside the training vocabulary are skipped rather than
        smoothed. Smoothing them adds the same constant to every class, so it
        changes no ranking — but it does drown the evidence list in words the
        model has never seen.
        """
        tokens = [t for t in tokenize(f"{subject} {body}") if t in self.vocabulary]
        if not tokens:
            return ClassificationResult(
                classification=Classification.NOISE, evidence="", confident=False
            )

        scores: dict[Classification, float] = {}
        for label, prior in self.log_prior.items():
            likelihood = self.log_likelihood[label]
            unseen = self.log_unseen[label]
            scores[label] = prior + sum(likelihood.get(token, unseen) for token in tokens)

        best = max(scores, key=lambda label: scores[label])
        return ClassificationResult(
            classification=best,
            evidence=self._evidence(tokens, best),
            # A posterior is a guess however peaked it is. `confident` is
            # reserved for the rule classifier, which is exact by construction.
            confident=False,
        )

    def _evidence(self, tokens: list[str], label: Classification) -> str:
        """The tokens that pushed hardest toward `label` and away from the rest.

        Not the highest P(token | label): common words score high in every
        class and would make every verdict cite the same four words. The
        margin over the runner-up class is what actually discriminated.
        """
        others = [other for other in self.log_prior if other is not label]
        if not others:
            return " ".join(sorted(set(tokens))[:EVIDENCE_TERMS])

        chosen = self.log_likelihood[label]
        chosen_unseen = self.log_unseen[label]

        def margin(token: str) -> float:
            mine = chosen.get(token, chosen_unseen)
            best_other = max(
                self.log_likelihood[other].get(token, self.log_unseen[other]) for other in others
            )
            return mine - best_other

        ranked = sorted(set(tokens), key=margin, reverse=True)
        return " ".join(ranked[:EVIDENCE_TERMS])


def train_from_corpus() -> NaiveBayesClassifier:
    """Fit on the seed corpus shipped with the package.

    Separate from `train` so the seed data has exactly one caller and swapping
    it for the owner's real mail is a one-line change.
    """
    from packages.inbox.corpus import TRAINING_CORPUS

    model = NaiveBayesClassifier.train(list(TRAINING_CORPUS))
    log.info("bayes_trained", examples=len(TRAINING_CORPUS), vocabulary=len(model.vocabulary))
    return model
