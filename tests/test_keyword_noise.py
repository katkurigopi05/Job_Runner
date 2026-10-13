"""A grammar word is not a requirement, and part of a word is not support.

Sorting one posting's terms against the owner's résumé on 2026-10-13 listed
"how" among the 36 things the posting asks for, and then found it
*supported*, by a GitHub project whose description uses the word. Two layers
were responsible, and the second was the worse.

**The term list held grammar.** `ats._requirements` drops a posting's
furniture with `matching.score._BOILERPLATE`, which has "what", "when",
"where", "which", "who" and "why" and not "how". Over 300 of the owner's open
postings 190 of about 10,280 scored terms were closed-class words, by the
tagger: "through" in 69 of them, "how" in 49, "like", "between", "behind",
"throughout", "per", "via", "whether". A résumé is not short of the word
"through". After the change, none; and against the owner's base résumé the
terms counted as backed across those postings went from 2,113 to 1,735.

**Support was read from inside words.** `keywords._supported` looked for a
term anywhere in the résumé's text, and the text of the owner's résumé says
"Linux", "Random Forest" and "cross-encoder". So "user experience" was
supported (`ux`), and "RESTful APIs" (`rest`), and "code". That is not only a
count that reads high. The supported terms are what the ordinary rewrite
hands a model as *safe to work in*, and they are left off the list of terms a
rewrite may not take from the posting. A model was told "user experience"
was safe, wrote it into a bullet about a command-line tool, and the guard,
which reads "user" and "experience" as ordinary English, kept it. That
résumé was never attached to anything.
"""

from __future__ import annotations

import pytest

from packages.core.models import Project
from packages.llm.provider import StubProvider
from packages.tailor import ats
from packages.tailor.guard import SourceCorpus
from packages.tailor.keywords import _supported, analyze
from packages.tailor.parse import ParsedResume, parse_text
from packages.tailor.rewrite import tailor_bullets
from packages.tailor.targeted import Status, classify, posting_terms
from tests.test_targeted_tailoring import CLASSIFIER, HANDBOOK, LOADER, by_term, resume

# --------------------------------------------------------------------------
# Grammar in the list of what a posting asks for
# --------------------------------------------------------------------------

#: The sentence "how" came from, with the employer's name taken out.
OPERATES = (
    "Requirements:\n"
    "Build intelligent internal systems that improve how the team operates.\n"
    "Ship classification models through CI/CD and know whether a release is safe.\n"
    "Work between finance and engineering, like a partner, throughout the quarter.\n"
)


def lowered(terms: list[str]) -> set[str]:
    return {term.lower() for term in terms}


@pytest.mark.parametrize("word", ["how", "through", "whether", "between", "like", "throughout"])
def test_a_grammar_word_is_not_a_requirement(word: str) -> None:
    assert word not in lowered(ats.requirement_terms(OPERATES))


def test_the_words_beside_it_still_are() -> None:
    terms = lowered(ats.requirement_terms(OPERATES))

    for kept in ("classification", "models", "systems", "finance", "ci/cd", "release"):
        assert kept in terms, kept


def test_technical_terms_identifiers_and_phrases_are_untouched() -> None:
    """The list as it was before either change, to the term and in order."""
    posting = (
        "Requirements:\n"
        "C++ and C# services, Node.js tooling, gRPC and GraphQL APIs.\n"
        "PostgreSQL, Redis and Kafka in production; Terraform on AWS.\n"
        "Machine learning pipelines, CI/CD, OAuth2 and test-driven development.\n"
    )

    assert ats.requirement_terms(posting) == [
        "machine learning",
        "C++",
        "services",
        "Node.js",
        "tooling",
        "gRPC",
        "GraphQL",
        "APIs",
        "PostgreSQL",
        "Redis",
        "Kafka",
        "production",
        "Terraform",
        "AWS",
        "pipelines",
        "CI/CD",
        "OAuth2",
        "test-driven",
    ]


def test_the_score_is_not_counted_over_grammar() -> None:
    """The denominator. Before, "how" and "through" were two of the terms a
    résumé was held to and could never be said to cover."""
    parsed = parse_text("Experience\nShipped classification models to production.\n")

    report = ats.score(parsed, OPERATES)

    counted = lowered(report.supported + report.missing)
    assert not counted & {"how", "through", "whether", "between", "like", "throughout"}
    assert "classification" in lowered(report.supported)


def test_how_does_not_become_a_target() -> None:
    """What happened: a project's description says "how", and the term was
    reported as supported by it. It is not a term at all now."""
    project = Project(
        source="github",
        external_id="1",
        name="photo-editor",
        url="https://github.com/owner/photo-editor",
        description="Shows how non-destructive filters compose in a browser.",
        language="TypeScript",
        topics_json=["webgpu"],
    )
    parsed = resume([CLASSIFIER, LOADER, HANDBOOK])

    plan = classify(parsed, OPERATES, projects=[project])

    assert "how" not in lowered(posting_terms(OPERATES))
    assert "how" not in by_term(plan)
    assert not [term for term in plan.terms if term.via == "photo-editor"]


# --------------------------------------------------------------------------
# Support read from inside a word
# --------------------------------------------------------------------------

LINES = """Experience
Built dashboards in JavaScript on Linux for the search team.
Trained a Random Forest and a cross-encoder to rank results.
Shipped the UI on a Kubernetes-based platform with a Postgres store.
"""


@pytest.fixture
def corpus() -> SourceCorpus:
    return SourceCorpus.from_resume(parse_text(LINES))


@pytest.mark.parametrize(
    "term, inside",
    [
        ("Java", "JavaScript"),
        ("user experience", "Linux"),  # by its alias `ux`
        ("RESTful APIs", "Forest"),  # by its alias `rest`
        ("code", "cross-encoder"),
        ("how", "dashboards"),  # d-a-s-h-b-o-a-r-d-s has none; "how" is in no word here
    ],
)
def test_part_of_a_word_is_not_support(corpus, term: str, inside: str) -> None:
    assert not _supported(term, corpus), f"{term!r} read out of {inside!r}"


@pytest.mark.parametrize(
    "term",
    [
        "JavaScript",
        "Linux",
        "Random Forest",
        "cross-encoder",
        "Kubernetes",  # a word of "Kubernetes-based"
        "user interface",  # the alias the résumé does write: UI
        "PostgreSQL",  # and another: Postgres
        "dashboard",  # the singular of a word it has
    ],
)
def test_a_word_or_an_alias_the_resume_does_write_still_is(corpus, term: str) -> None:
    assert _supported(term, corpus), term


def test_the_split_the_model_is_handed_changes_with_it(corpus) -> None:
    posting = "Requirements: Java, user experience, RESTful APIs, JavaScript and Linux."

    terms = analyze(posting, corpus)

    assert {"java", "user experience", "restful apis"} <= lowered(terms.missing)
    assert {"javascript", "linux"} <= lowered(terms.supported)


async def test_a_term_backed_by_part_of_a_word_cannot_be_written_in() -> None:
    """The rewrite that happened. "User experience" was supported by the
    `ux` in Linux, so it was offered to the model and was not off limits, and
    the guard reads both its words as ordinary English."""
    bullet = "Built and published a command-line audit tool on Linux for every release."
    parsed = ParsedResume.model_validate(
        {
            "contact": {"name": "Fixture Owner", "email": "fixture@example.com", "links": []},
            "sections": {"experience": [bullet]},
            "raw_lines": ["Experience", bullet],
        }
    )
    written_in = (
        "Built and published a command-line audit tool on Linux for every release "
        "to support user experience."
    )
    posting = "Requirements: user experience and Linux."

    result = await tailor_bullets(
        StubProvider({"Built": written_in}), [bullet], posting, SourceCorpus.from_resume(parsed)
    )

    assert result.tailored_lines == [bullet]
    assert result.rejected == 1
    reason = result.bullets[0].rejected_reason
    assert reason is not None and "'user experience'" in reason


def test_the_practice_classification_is_unchanged() -> None:
    """The fixture the targeted tests are built on reads as it did."""
    posting = "Requirements:\nClassification, automated testing, and Kubernetes.\n"
    plan = classify(resume([CLASSIFIER, LOADER, HANDBOOK]), posting)

    terms = by_term(plan)
    assert terms["classification"].status is Status.COVERED
    assert terms["automated testing"].status is Status.SUPPORTED
    assert terms["kubernetes"].status is Status.GAP
