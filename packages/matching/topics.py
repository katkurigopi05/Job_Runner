"""Latent Dirichlet Allocation over posting text, and what its entropy says.

Ported from TA-GAI's `LDA_implementation.py` — collapsed Gibbs sampling, no
gensim and no scikit-learn. scikit-learn is present in the tree only as a
transitive dependency of sentence-transformers, and building a load-bearing
feature on a dependency nobody declared is how a working install breaks on
somebody else's machine.

## What the entropy is for

`legitimacy.specificity()` already measures vagueness one way: the share of
tokens that are not boilerplate. That is a vocabulary question. This is a
different one — *how many subjects is this posting about at once*.

A real posting concentrates. It is about backend infrastructure, or about
clinical operations, and its topic distribution is peaked. A posting written
to catch every applicant spreads mass evenly across topics, and evenly spread
mass is exactly what high entropy means. The two measures disagree usefully:
a ghost posting stuffed with distinctive jargon from four unrelated fields
scores *well* on specificity and badly here.

## Why it is not computed inside `assess()`

`legitimacy.py` is explicit that everything in it is computed from the posting
already in hand, because discovery runs on a schedule over thousands of
postings. LDA has to be fit over a corpus before it can say anything about one
document, so fitting it per-posting would violate exactly that constraint.

So the model is fit offline (`scripts.fit_topics`) and *passed in*. When no
model is supplied the signal is simply absent, and `assess()` costs what it
cost before.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from dataclasses import dataclass

import structlog

from packages.matching.embed import tokenize

log = structlog.get_logger(__name__)

#: Dirichlet priors. Symmetric and conventional: alpha over document-topic,
#: beta over topic-word. Low alpha favours documents about few topics, which
#: is the shape a real job posting has.
ALPHA = 0.1
BETA = 0.01

DEFAULT_TOPICS = 12
DEFAULT_ITERATIONS = 200

#: A token appearing in fewer documents than this carries no topic signal and
#: inflates the vocabulary, which slows every sweep of the sampler.
MIN_DOCUMENT_FREQUENCY = 3


@dataclass(frozen=True)
class TopicModel:
    """A fitted model. Immutable so a fit cannot drift after the fact."""

    #: topic -> token -> count
    topic_word: tuple[Counter[str], ...]
    #: topic -> total tokens assigned
    topic_total: tuple[int, ...]
    vocabulary: frozenset[str]

    @property
    def topics(self) -> int:
        return len(self.topic_word)

    def transform(self, text: str, *, iterations: int = 50, seed: int = 0) -> list[float]:
        """Infer this document's topic mixture against the fitted topics.

        The topic-word counts stay frozen — this is inference for one unseen
        document, not a refit, so a posting scored today and the same posting
        scored tomorrow get the same answer.
        """
        tokens = [t for t in tokenize(text) if t in self.vocabulary]
        if not tokens:
            return [1.0 / self.topics] * self.topics

        rng = random.Random(seed)
        assignments = [rng.randrange(self.topics) for _ in tokens]
        doc_counts = Counter(assignments)
        vocabulary_size = len(self.vocabulary)

        for _ in range(iterations):
            for index, token in enumerate(tokens):
                current = assignments[index]
                doc_counts[current] -= 1

                weights = [
                    (doc_counts[topic] + ALPHA)
                    * (self.topic_word[topic][token] + BETA)
                    / (self.topic_total[topic] + BETA * vocabulary_size)
                    for topic in range(self.topics)
                ]
                chosen = _sample(weights, rng)
                assignments[index] = chosen
                doc_counts[chosen] += 1

        total = len(tokens) + ALPHA * self.topics
        return [(doc_counts[topic] + ALPHA) / total for topic in range(self.topics)]


def _sample(weights: list[float], rng: random.Random) -> int:
    """Draw an index proportional to `weights`."""
    total = sum(weights)
    if total <= 0:
        return rng.randrange(len(weights))
    threshold = rng.random() * total
    cumulative = 0.0
    for index, weight in enumerate(weights):
        cumulative += weight
        if cumulative >= threshold:
            return index
    return len(weights) - 1


def fit(
    documents: list[str],
    *,
    topics: int = DEFAULT_TOPICS,
    iterations: int = DEFAULT_ITERATIONS,
    seed: int = 0,
) -> TopicModel:
    """Collapsed Gibbs sampling.

    `seed` is not a convenience. An unseeded fit gives a different model every
    run, so the same posting would draw a different vagueness finding each
    time the corpus was refit — and a signal that moves on its own is worse
    than no signal.
    """
    if not documents:
        raise ValueError("cannot fit a topic model on an empty corpus")

    tokenized = [tokenize(document) for document in documents]

    document_frequency: Counter[str] = Counter()
    for tokens in tokenized:
        document_frequency.update(set(tokens))
    vocabulary = {
        token for token, count in document_frequency.items() if count >= MIN_DOCUMENT_FREQUENCY
    }
    if not vocabulary:
        raise ValueError(
            f"no token appears in {MIN_DOCUMENT_FREQUENCY}+ documents; corpus is too small"
        )

    tokenized = [[t for t in tokens if t in vocabulary] for tokens in tokenized]
    vocabulary_size = len(vocabulary)

    rng = random.Random(seed)
    assignments = [[rng.randrange(topics) for _ in tokens] for tokens in tokenized]

    topic_word: list[Counter[str]] = [Counter() for _ in range(topics)]
    topic_total = [0] * topics
    doc_topic = [Counter[int]() for _ in tokenized]

    for document_index, tokens in enumerate(tokenized):
        for token, topic in zip(tokens, assignments[document_index], strict=True):
            topic_word[topic][token] += 1
            topic_total[topic] += 1
            doc_topic[document_index][topic] += 1

    for _ in range(iterations):
        for document_index, tokens in enumerate(tokenized):
            counts = doc_topic[document_index]
            for position, token in enumerate(tokens):
                current = assignments[document_index][position]
                counts[current] -= 1
                topic_word[current][token] -= 1
                topic_total[current] -= 1

                weights = [
                    (counts[topic] + ALPHA)
                    * (topic_word[topic][token] + BETA)
                    / (topic_total[topic] + BETA * vocabulary_size)
                    for topic in range(topics)
                ]
                chosen = _sample(weights, rng)

                assignments[document_index][position] = chosen
                counts[chosen] += 1
                topic_word[chosen][token] += 1
                topic_total[chosen] += 1

    log.info(
        "topic_model_fitted", documents=len(documents), vocabulary=vocabulary_size, topics=topics
    )
    return TopicModel(
        topic_word=tuple(topic_word),
        topic_total=tuple(topic_total),
        vocabulary=frozenset(vocabulary),
    )


def entropy(distribution: list[float]) -> float:
    """Shannon entropy normalised to [0, 1] by the maximum for this many topics.

    Normalised so the number means the same thing across models fitted with
    different topic counts. 0 is a document wholly about one topic; 1 is one
    spread evenly across all of them.
    """
    if not distribution:
        return 0.0
    total = sum(distribution)
    if total <= 0:
        return 0.0

    probabilities = [p / total for p in distribution if p > 0]
    raw = -sum(p * math.log(p) for p in probabilities)
    ceiling = math.log(len(distribution))
    return raw / ceiling if ceiling > 0 else 0.0


def top_terms(model: TopicModel, topic: int, *, limit: int = 8) -> list[str]:
    """The words that define a topic. For reading a fitted model, not scoring."""
    return [token for token, _ in model.topic_word[topic].most_common(limit)]
