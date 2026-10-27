"""Place spans preserve the original question for separate location ranking."""

import pytest

from packages.matching import locality


@pytest.mark.parametrize(
    "surface",
    ["San Francisco", "SF", "Bay Area", "San Jose", "Mountain View", "South San Francisco"],
)
def test_bay_place_span_covers_the_complete_surface(surface):
    question = f"software engineer jobs in {surface} using Kafka"
    start = question.index(surface)
    assert locality.bay_area_place_spans(question) == ((start, start + len(surface)),)


def test_place_spans_retain_case_punctuation_and_original_unicode_offsets():
    question = "résumé: jobs in (SAN FRANCISCO), or sf?"
    spans = locality.bay_area_place_spans(question)
    assert tuple(question[start:end] for start, end in spans) == ("SAN FRANCISCO", "sf")
    assert spans == ((17, 30), (36, 38))


def test_consuming_spans_preserves_the_actual_search_subject():
    question = "Kafka jobs in San Francisco or San Jose"
    spans = locality.bay_area_place_spans(question)
    consumed = question
    for start, end in reversed(spans):
        consumed = consumed[:start] + " " + consumed[end:]
    assert consumed == "Kafka jobs in   or  "


@pytest.mark.parametrize("question", ["Kafka jobs", "jobs using SFML", "Bay Areaish jobs", ""])
def test_unrecognized_and_partial_place_words_have_no_spans(question):
    assert locality.bay_area_place_spans(question) == ()


def test_span_lookup_does_not_change_country_or_state_precedence():
    question = "Newark, NJ"
    before = locality.locality_of(question)
    assert locality.bay_area_place_spans(question) == ()
    assert locality.locality_of(question) is before is locality.Locality.UNITED_STATES
