"""LDA over posting text, and the vagueness signal built on its entropy.

Ported from TA-GAI's `LDA_implementation.py`. The interesting property is not
that LDA works — it is that entropy disagrees with `specificity()` in a useful
direction, so the two together catch a posting neither catches alone.
"""

from __future__ import annotations

import pytest

from packages.core.models import Posting
from packages.matching.legitimacy import Weight, _topic_focus, assess, specificity
from packages.matching.topics import entropy, fit, top_terms

# Three clearly separate subjects, repeated enough for a topic model to find
# them. Short and synthetic on purpose: this tests the sampler, not the corpus.
BACKEND = [
    "python postgresql backend services api latency",
    "backend python api postgresql throughput services",
    "services api backend python postgresql database",
    "python api services database backend latency",
]
CLINICAL = [
    "clinical nursing patient care hospital ward",
    "patient nursing clinical hospital care rounds",
    "nursing hospital patient clinical care ward",
    "clinical patient care nursing hospital rounds",
]
CULINARY = [
    "kitchen chef menu restaurant cooking service",
    "chef kitchen restaurant menu cooking prep",
    "restaurant kitchen chef cooking menu prep",
    "menu chef kitchen restaurant cooking service",
]

CORPUS = BACKEND + CLINICAL + CULINARY


@pytest.fixture(scope="module")
def model():
    return fit(CORPUS, topics=3, iterations=120, seed=7)


def test_a_document_about_one_subject_has_low_entropy(model) -> None:
    focused = model.transform("python postgresql backend api services latency")
    assert entropy(focused) < 0.9


def test_a_document_mixing_every_subject_has_higher_entropy(model) -> None:
    """The signal the vagueness check is built on."""
    focused = model.transform("python postgresql backend api services latency")
    mixed = model.transform(
        "python postgresql backend nursing patient hospital chef kitchen menu restaurant"
    )
    assert entropy(mixed) > entropy(focused)


def test_entropy_is_normalised_so_topic_counts_stay_comparable() -> None:
    assert entropy([1.0, 0.0, 0.0]) == pytest.approx(0.0)
    assert entropy([0.25] * 4) == pytest.approx(1.0)
    assert entropy([]) == 0.0
    assert entropy([0.0, 0.0]) == 0.0


def test_a_fit_is_reproducible() -> None:
    """An unseeded fit would move the vagueness finding on every refit."""
    first = fit(CORPUS, topics=3, iterations=60, seed=11)
    second = fit(CORPUS, topics=3, iterations=60, seed=11)
    assert [top_terms(first, k) for k in range(3)] == [top_terms(second, k) for k in range(3)]


def test_an_empty_corpus_is_refused() -> None:
    with pytest.raises(ValueError, match="empty corpus"):
        fit([])


def test_a_corpus_too_small_to_have_shared_vocabulary_is_refused() -> None:
    with pytest.raises(ValueError, match="too small"):
        fit(["one two three", "four five six"], topics=2, iterations=5)


def test_unknown_text_falls_back_to_a_uniform_mixture(model) -> None:
    """No shared vocabulary means no evidence, and evidence-free must not
    masquerade as a confident single-topic answer."""
    assert model.transform("zzz qqq") == pytest.approx([1 / 3] * 3)


def test_the_signal_disagrees_with_specificity_where_it_should(model) -> None:
    """Why this earns its place beside the existing vagueness measure.

    A posting stuffed with distinctive jargon from unrelated fields scores
    *well* on specificity — every term is distinctive — and badly here,
    because those terms do not describe one job.
    """
    text = (
        "python postgresql backend api services latency nursing patient hospital "
        "clinical ward chef kitchen menu restaurant cooking prep rounds"
    )
    posting = Posting(url="https://example.com/x", title="Associate", description_raw=text)

    assert specificity(text) > 0.5, "every term here is distinctive"
    assert _topic_focus(posting, model).weight is Weight.CONCERNING


def test_assess_is_unchanged_when_no_model_is_supplied() -> None:
    """The cost guarantee: legitimacy.assess stays what it was."""
    posting = Posting(
        url="https://example.com/y",
        title="Backend Engineer",
        description_raw=" ".join(BACKEND * 12),
    )

    without = assess(posting)
    assert not any(signal.name == "topic_focus" for signal in without.signals)


def test_assess_gains_the_signal_when_a_model_is_supplied(model) -> None:
    posting = Posting(
        url="https://example.com/z",
        title="Backend Engineer",
        description_raw=" ".join(BACKEND * 12),
    )

    with_model = assess(posting, topics=model)
    assert any(signal.name == "topic_focus" for signal in with_model.signals)
