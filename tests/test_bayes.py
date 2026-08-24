"""A Naive Bayes baseline for inbound classification.

Ported from TA-GAI's `nb_implementation.py` — multinomial Naive Bayes with
Laplace smoothing, hand-rolled rather than pulled from scikit-learn, because
the whole value here is that the verdict can be explained token by token.

## Why a second classifier at all

`classify.py` is rules, then a model when the rules abstain. The rules are
exact and the model is a black box, and there is nothing in between: no way
to answer "how hard is this problem, actually" without asking Ollama. A
Bayes baseline is that middle. It is local, deterministic, costs nothing, and
gives Gate 6's accuracy number something to be measured *against* — a number
with no baseline is not a result.

## The training set is not the evaluation set

`tests/test_inbox.py::LABELED` is what Gate 6 scores against. Training on it
would make the accuracy meaningless, so the corpus in `packages/inbox/corpus.py`
is disjoint and `test_the_training_corpus_does_not_overlap_the_gate_set`
enforces that. Both are synthetic — CLAUDE.md §15 makes the same admission
about the gate set — so the number below measures the method, not the mail.
"""

from __future__ import annotations

from packages.core.enums import Classification
from packages.inbox.bayes import NaiveBayesClassifier, train_from_corpus
from packages.inbox.corpus import TRAINING_CORPUS
from tests.test_inbox import LABELED


def test_it_learns_a_class_from_its_vocabulary() -> None:
    model = NaiveBayesClassifier.train(
        [
            (Classification.REJECTION, "unfortunately we will not be proceeding"),
            (Classification.REJECTION, "regret to inform you the role is filled"),
            (Classification.INTERVIEW, "we would like to schedule a call with you"),
            (Classification.INTERVIEW, "are you available for an interview thursday"),
        ]
    )

    assert model.classify("", "unfortunately the role is filled").classification is (
        Classification.REJECTION
    )
    assert model.classify("", "available to schedule a call").classification is (
        Classification.INTERVIEW
    )


def test_an_unseen_word_does_not_zero_the_posterior() -> None:
    """What the Laplace smoothing is for.

    Without it a single token absent from a class's training vocabulary drives
    that class's probability to zero no matter how much the rest of the message
    supports it.
    """
    model = NaiveBayesClassifier.train(
        [
            (Classification.REJECTION, "unfortunately not proceeding"),
            (Classification.INTERVIEW, "schedule a call"),
        ]
    )

    result = model.classify("", "unfortunately not proceeding quetzalcoatl")
    assert result.classification is Classification.REJECTION


def test_it_reports_the_tokens_that_drove_the_verdict() -> None:
    """A verdict with no evidence is not auditable, and every other
    classifier in this package carries its evidence."""
    model = train_from_corpus()
    result = model.classify("Update on your application", "We regret to inform you.")

    assert result.evidence, "the verdict should name what produced it"
    assert not result.confident, "a probabilistic guess is never `confident`"


def test_the_training_corpus_does_not_overlap_the_gate_set() -> None:
    """Train/test contamination would make Gate 6's number a fiction."""
    trained = {body.strip().lower() for _, body in TRAINING_CORPUS}
    gate = {body.strip().lower() for _, _, body in LABELED}

    assert not (trained & gate), "training corpus overlaps the Gate 6 evaluation set"


def test_the_baseline_beats_chance_on_the_gate_set() -> None:
    """The point of a baseline: a number Gate 6's LLM has to beat.

    Deliberately not asserted at 90%. This is a bag-of-words model trained on
    a few dozen synthetic messages, and pinning it high would only invite
    tuning the corpus until the assertion passes — which measures nothing.
    Chance across 7 classes is ~14%.
    """
    model = train_from_corpus()
    correct = sum(
        model.classify(subject, body).classification is expected
        for expected, subject, body in LABELED
    )
    accuracy = correct / len(LABELED)

    print(f"\nNaive Bayes baseline on the Gate 6 set: {accuracy:.0%} ({correct}/{len(LABELED)})")
    assert accuracy > 0.4, f"baseline no better than a coin flip: {accuracy:.0%}"
