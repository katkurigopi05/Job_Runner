"""Abbreviations in job titles.

Measured before this existed, against LexicalEmbedder:

    Site Reliability Engineer  vs  SRE          0.000
    Senior Backend Engineer    vs  Pastry Chef  0.000

The same job scored exactly as similar as an unrelated one.
"""

from __future__ import annotations

import pytest

from packages.matching.embed import LexicalEmbedder, cosine
from packages.matching.titles import ALIASES, canonical, expand


def _similarity(a: str, b: str) -> float:
    embedder = LexicalEmbedder()
    return cosine(*embedder.encode([expand(a), expand(b)]))


@pytest.mark.parametrize(
    ("abbreviated", "written_out"),
    [
        ("SRE", "Site Reliability Engineer"),
        ("SDE 2", "Software Engineer II"),
        ("ML Engineer", "Machine Learning Engineer"),
        ("Sr. Data Engineer", "Senior Data Engineer"),
        ("QA Lead", "Quality Assurance Lead"),
    ],
)
def test_an_abbreviated_title_now_matches_what_it_abbreviates(
    abbreviated: str, written_out: str
) -> None:
    assert _similarity(abbreviated, written_out) > 0.5


def test_an_unrelated_title_still_does_not_match() -> None:
    """The check that the fix did not just make everything similar."""
    assert _similarity("Senior Backend Engineer", "Pastry Chef") == 0.0


def test_expansion_keeps_the_original_word() -> None:
    """Dropping it would lose the match against a posting that also writes
    "SRE", which is the more common case."""
    expanded = expand("SRE").lower()

    assert "sre" in expanded
    assert "site reliability engineer" in expanded


def test_a_roman_numeral_is_replaced_not_added() -> None:
    """ "II" and "2" are two spellings of one level. Keeping both would make
    the title look like it mentions two."""
    expanded = expand("Engineer II")

    assert "2" in expanded
    assert "II" not in expanded


def test_dotted_abbreviations_are_read() -> None:
    assert "senior" in expand("Sr. Engineer").lower()


def test_an_ambiguous_abbreviation_is_left_alone() -> None:
    """ "PM" is product manager or project manager depending on the company.
    Guessing turns a title into the wrong job rather than a fuzzy one."""
    assert "pm" not in ALIASES
    assert expand("PM") == "PM"


def test_expansion_of_nothing_is_nothing() -> None:
    assert expand(None) == ""
    assert expand("") == ""


def test_canonical_ignores_word_order() -> None:
    """ "Engineer, Backend" and "Backend Engineer" name one job."""
    assert canonical("Backend Engineer") == canonical("Engineer, Backend")


def test_canonical_sees_through_an_abbreviation() -> None:
    assert canonical("Sr. SWE") == canonical("Senior Software Engineer")


def test_a_resume_written_in_abbreviations_does_not_report_false_gaps() -> None:
    """Unexpanded, a résumé saying "ML" was told it was missing "machine"."""
    import uuid

    from packages.core.models import Posting
    from packages.matching.score import missing_terms

    posting = Posting(
        id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        title="Machine Learning Engineer",
        url="https://boards.greenhouse.io/acme/jobs/1",
        description_raw="Machine learning at scale. Machine learning pipelines.",
    )
    resume = "Jane Doe. Built ML pipelines in Python."

    assert "machine" in missing_terms(resume, posting)
    assert "machine" not in missing_terms(expand(resume), posting)
