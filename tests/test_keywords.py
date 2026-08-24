"""Which of a posting's vocabulary the résumé actually backs.

§2.1 permits injecting keywords "already supported by the source résumé".
Nothing computed that set — the whole description went to the model with an
instruction not to invent, and the instruction was the only safeguard.
"""

from __future__ import annotations

from packages.tailor.guard import SourceCorpus
from packages.tailor.keywords import analyze, job_terms

RESUME = """Jordan Avery
Senior Engineer, Example Corp
Built backend services in Python.
Worked on reliability for internal tools.
Skills: Python, PostgreSQL, FastAPI"""

JD = """Senior Backend Engineer, Payments. You will own high-throughput payment
services in Python, improve reliability of a critical path, and work with
PostgreSQL at scale. Kubernetes and Kafka experience preferred."""


def test_supported_terms_are_ones_the_resume_backs() -> None:
    report = analyze(JD, SourceCorpus.from_texts(RESUME))

    assert "Python" in report.supported
    assert "PostgreSQL" in report.supported
    assert "reliability" in report.supported


def test_terms_the_resume_lacks_are_named_not_hidden() -> None:
    """They are exactly what a model reaches for when it invents."""
    report = analyze(JD, SourceCorpus.from_texts(RESUME))

    assert "Kubernetes" in report.missing
    assert "Kafka" in report.missing
    assert "Kubernetes" not in report.supported


def test_role_words_are_never_offered_for_injection() -> None:
    """ "Senior Engineer" is supported and injecting it ruins the bullet.

    An earlier version offered them, and the model turned "Built backend
    services in Python" into "Senior Backend Engineer in Python, built
    services". The guard accepted it — nothing was fabricated — and it was
    still ruined. A bullet describes the work; the title belongs in the header.
    """
    report = analyze(JD, SourceCorpus.from_texts(RESUME))
    offered = {term.lower() for term in report.supported + report.missing}

    assert not ({"senior", "engineer", "staff", "principal"} & offered)


def test_the_guards_common_words_are_not_reused_as_stopwords() -> None:
    """Two questions, two lists.

    The guard's `_COMMON_WORDS` answers "is this token weak evidence of
    fabrication" and contains `reliability`, `services`, `backend`. Reusing it
    reported a résumé saying all three as supporting none of them.
    """
    terms = [t.lower() for t in job_terms(JD)]

    assert "reliability" in terms
    assert "services" in terms


def test_coverage_counts_vocabulary_not_meaning() -> None:
    report = analyze(JD, SourceCorpus.from_texts(RESUME))

    assert 0.0 < report.coverage < 1.0
    assert report.coverage == round(
        len(report.supported) / (len(report.supported) + len(report.missing)), 3
    )


def test_an_empty_posting_yields_nothing() -> None:
    report = analyze("", SourceCorpus.from_texts(RESUME))

    assert report.supported == []
    assert report.missing == []
    assert report.coverage == 0.0
