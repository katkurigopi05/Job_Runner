"""The feed's keyword filter: whole words, nearby words, and the words around a hit.

The owner searched "software engineer ai" on 2026-10-20 and the filter, a
substring test, answered badly both ways. Typed as one phrase it named the
software engineer role and dropped "ai": 542 software engineer titles, mostly
not AI. Typed as two keywords, "ai" matched inside "maintain", "container"
and "availability": 199 of 927 results.
"""

from __future__ import annotations

from packages.matching.keywords import find, parse
from packages.matching.search import SearchFilters, matches
from tests.test_roles import posting


def _hit(keyword: str, title: str = "Role", description: str = "", location: str = ""):
    return find(parse(keyword), posting(title=title, description=description, location=location))


# --- whole words ----------------------------------------------------------------------


def test_a_keyword_does_not_match_inside_another_word() -> None:
    assert _hit("ml", description="We build HTML and XML tools.") is None
    assert _hit("ai", description="You will maintain our container fleet.") is None
    assert _hit("go", description="Ago, Google was good.") is None
    assert _hit("java", description="We write JavaScript.") is None


def test_a_whole_word_matches_in_any_case() -> None:
    assert _hit("ai", description="We are an AI company.") is not None
    assert _hit("python", description="Python, Postgres.") is not None


def test_names_with_symbols_still_match() -> None:
    assert _hit("c++", description="Strong C++ skills.") is not None
    assert _hit("c#", description="C# and .NET services.") is not None
    assert _hit(".net", description="C# and .NET services.") is not None
    assert _hit("node.js", description="We run Node.js.") is not None
    assert _hit("c", description="Strong C++ skills.") is None


def test_a_plural_counts() -> None:
    assert _hit("engineer", description="Join our engineers.") is not None


# --- phrases and nearby words ---------------------------------------------------------


def test_a_phrase_matches_where_it_is_written() -> None:
    assert _hit("machine learning", description="Experience with machine learning.") is not None


def test_words_close_together_match_in_any_order() -> None:
    for title in (
        "AI Software Engineer",
        "Software Engineer, AI Platform",
        "Software Engineer (AI Infrastructure)",
        "Senior Software Engineer - Cortex AI Infrastructure",
    ):
        assert _hit("software engineer ai", title=title) is not None, title


def test_words_far_apart_do_not_match() -> None:
    far = "Our software engineer team ships payments; separately, we evaluate AI vendors."
    assert _hit("software engineer ai", title="Platform Engineer", description=far) is None


def test_two_words_with_two_between_still_match_in_a_title() -> None:
    assert _hit("machine learning", title="Machine and Deep Learning Engineer") is not None
    assert _hit("machine learning", title="Machine Vision, Robotics and Learning Lead") is None


def test_in_a_description_the_words_must_stand_as_written() -> None:
    """Nearness in a description found one company's boilerplate on 20
    unrelated postings, and words either side of a full stop."""
    boilerplate = "Millions of open source software engineers and AI agents rely on it."
    assert _hit("software engineer ai", title="Account Executive", description=boilerplate) is None
    across = "You will work with engineers. Production software ships daily."
    assert _hit("software engineer", title="Account Executive", description=across) is None
    assert _hit("machine learning", description="audio-based machine-learning workflows")


def test_a_phrase_deep_in_a_long_description_is_found_and_shown() -> None:
    filler = "We value craft and care. " * 40
    hit = _hit(
        "event streaming", description=f"{filler}You will own event streaming at scale. {filler}"
    )

    assert hit is not None
    assert (hit.before, hit.match, hit.after) == ("… will own", "event streaming", "at scale …")


def test_words_with_nothing_between_them_still_match_as_a_phrase() -> None:
    assert _hit("c# .net", description="Five years of C#.NET services.") is not None


def test_a_window_does_not_read_half_a_word_as_a_word() -> None:
    from packages.matching.keywords import _tokens_between

    text = "see docs.ai today"
    # A window starting inside "docs.ai" must not read ".ai" or "ai" from it.
    assert [t.word for t in _tokens_between(text, text.index(".ai"), len(text))] == ["today"]


def test_a_word_after_a_long_run_of_punctuation_is_still_read() -> None:
    hit = _hit("kafka", description="=" * 400 + " Kafka pipelines")

    assert hit is not None and hit.match == "Kafka"


# --- roles ------------------------------------------------------------------------------


def test_a_role_name_still_finds_its_synonyms() -> None:
    assert _hit("software engineer", title="Member of Technical Staff") is not None


def test_a_role_and_a_word_needs_the_word_in_the_title() -> None:
    """'software engineer ai' named the role and lost 'ai': 542 results."""
    assert _hit("software engineer ai", title="Member of Technical Staff, AI Engineering")
    assert _hit("software engineer ai", title="Member of Technical Staff - Inference") is None
    assert _hit("software engineer ai", title="Software Engineer - Postgres") is None


def test_a_role_name_does_not_match_a_different_role() -> None:
    assert _hit("data engineer", title="Data Scientist", description="Statistics.") is None


# --- the words around a hit -------------------------------------------------------------


def test_a_hit_shows_two_words_either_side() -> None:
    hit = _hit(
        "kafka",
        title="Backend Engineer",
        description="You will design event pipelines with Kafka and Flink for our ads team.",
    )

    assert hit is not None
    assert (hit.field, hit.before, hit.match, hit.after) == (
        "description",
        "… pipelines with",
        "Kafka",
        "and Flink …",
    )


def test_a_hit_at_the_start_has_no_ellipsis() -> None:
    hit = _hit("kafka", description="Kafka experience required.")

    assert hit is not None and (hit.before, hit.after) == ("", "experience required.")


def test_a_title_hit_is_shown_from_the_title() -> None:
    hit = _hit("software engineer ai", title="Software Engineer, AI Platform", description="AI.")

    assert hit is not None and hit.field == "title"
    assert hit.match == "Software Engineer, AI"


def test_a_synonym_hit_shows_the_whole_title() -> None:
    hit = _hit("software engineer", title="Member of Technical Staff")

    assert hit is not None
    assert (hit.field, hit.match) == ("title", "Member of Technical Staff")


# --- through the feed's filter ----------------------------------------------------------


def test_the_filter_carries_each_keywords_hit() -> None:
    verdict = matches(
        posting(title="AI Software Engineer", description="We use Python daily."),
        SearchFilters(keywords=("software engineer ai", "python")),
    )

    assert verdict.kept
    assert [hit.keyword for hit in verdict.hits] == ["software engineer ai", "python"]


def test_a_missing_keyword_still_says_which() -> None:
    verdict = matches(
        posting(title="Backend Engineer", description="We maintain containers."),
        SearchFilters(keywords=("ai",)),
    )

    assert not verdict.kept
    assert "missing keyword 'ai'" in verdict.reasons
