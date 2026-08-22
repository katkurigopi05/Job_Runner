"""Fabrication by recombination — true words, untrue arrangement.

The gap this fills is structural, not a bug in `guard.py`. A token-level check
has no representation of which words stood next to which, so it cannot see a
claim assembled entirely from supported vocabulary.
"""

from __future__ import annotations

from packages.tailor.guard import SourceCorpus, check
from packages.tailor.recombination import find

# Two employers. Kubernetes belongs to one, cluster administration to the
# other, and the résumé never claims the two together.
RESUME = """
Acme Corp — Backend Engineer
Built services in Python. Deployed to Kubernetes.

Globex — Systems Administrator
Responsible for cluster administration on bare metal.
"""


def test_the_existing_guard_passes_a_recombined_claim() -> None:
    """Not a criticism of the guard — the reason this module exists.

    Every token below traces to the résumé, so a token-level check is correct
    to pass it, and the sentence still asserts something never claimed.
    """
    corpus = SourceCorpus.from_texts(RESUME)
    report = check("Kubernetes cluster administration at Acme.", corpus)

    assert report.ok, "token checking passes this; that is the premise"


def test_recombination_is_caught() -> None:
    corpus = SourceCorpus.from_texts(RESUME)
    findings = find("Kubernetes cluster administration at Acme.", corpus)

    pairs = {(f.first.normalized, f.second.normalized) for f in findings}
    assert ("kubernetes", "cluster") in pairs


def test_words_that_did_stand_together_are_not_reported() -> None:
    """The false-positive case that decides whether this is usable."""
    corpus = SourceCorpus.from_texts(RESUME)
    assert find("Cluster administration at Globex.", corpus) == []


def test_an_intervening_ordinary_word_breaks_adjacency() -> None:
    """ "Kubernetes and cooking" is not the assertion "Kubernetes cooking"."""
    corpus = SourceCorpus.from_texts(RESUME)
    findings = find("Kubernetes and cluster work.", corpus)

    pairs = {(f.first.normalized, f.second.normalized) for f in findings}
    assert ("kubernetes", "cluster") not in pairs


def test_an_unsupported_token_is_left_to_the_guard() -> None:
    """Reporting it twice under two names makes the output harder to read,
    not safer."""
    corpus = SourceCorpus.from_texts(RESUME)
    findings = find("Terraform modules at Acme.", corpus)

    assert all("terraform" not in (f.first.normalized, f.second.normalized) for f in findings), (
        "an unsupported token is a guard violation, not a recombination"
    )


def test_it_never_changes_the_guard_verdict() -> None:
    """Ships as a report. Promoting it to a rejection needs a measured
    false-positive rate first."""
    corpus = SourceCorpus.from_texts(RESUME)
    text = "Kubernetes cluster administration at Acme."

    assert check(text, corpus).ok
    assert find(text, corpus), "found something, and the verdict still stands"
