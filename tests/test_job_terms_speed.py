"""Freeze the ordered tailoring terms and support decisions before optimization."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from packages.github import select as project_rank
from packages.matching.requirements import PostingRequirements
from packages.tailor import keywords
from packages.tailor.aliases import equivalents, known_phrases
from packages.tailor.guard import SourceCorpus, normalize, singular, stem
from tests.test_ats_score import _NARRATIVE_POSTING
from tests.test_keywords import DEV_JD, JD, RESUME


def reference_known_phrases(job_description: str) -> tuple[list[str], set[str]]:
    normalized = " ".join(normalize(raw) for raw in keywords._TOKEN_RE.findall(job_description))
    found: list[str] = []
    consumed: set[str] = set()
    claimed: set[frozenset[str]] = set()
    for phrase in known_phrases():
        if phrase not in normalized:
            continue
        group = equivalents(phrase)
        if group in claimed:
            continue
        claimed.add(group)
        found.append(phrase)
        for word in phrase.split():
            consumed.add(word)
            consumed.add(singular(word))
    return found, consumed


def reference_job_terms(job_description: str, *, limit: int = 40) -> list[str]:
    phrases, consumed = reference_known_phrases(job_description)
    counts: Counter[str] = Counter()
    surface: dict[str, str] = {}
    for raw in keywords._TOKEN_RE.findall(job_description):
        key = normalize(raw)
        if not key or len(key) < 3:
            continue
        if key in keywords._STOPWORDS or key in keywords._ROLE_WORDS:
            continue
        if stem(key) in keywords._VERB_STEMS or key in consumed:
            continue
        counts[key] += 1
        surface.setdefault(key, raw.strip(".,;:"))
    singles = [surface[key] for key, _ in counts.most_common(limit)]
    return (phrases + singles)[:limit]


def reference_analyze(
    job_description: str, corpus: SourceCorpus, *, limit: int = 40
) -> keywords.TermReport:
    supported, missing = [], []
    for term in reference_job_terms(job_description, limit=limit):
        (supported if keywords._supported(term, corpus) else missing).append(term)
    return keywords.TermReport(supported=supported, missing=missing)


GOLDEN = json.loads((Path(__file__).parent / "fixtures/golden/postings.json").read_text())
POSTINGS = [pytest.param(row["description"], id=row["title"]) for row in GOLDEN["postings"]]


@pytest.mark.parametrize("posting", [*POSTINGS, JD, DEV_JD, _NARRATIVE_POSTING])
@pytest.mark.parametrize("limit", [-1, 0, 1, 8, 40, 100])
def test_job_terms_and_analysis_match_reference_in_order(posting: str, limit: int) -> None:
    corpus = SourceCorpus.from_texts(RESUME, "ML, Airflow data pipelines, CI/CD and AWS")
    assert keywords._known_phrases(posting) == reference_known_phrases(posting)
    assert keywords.job_terms(posting, limit=limit) == reference_job_terms(posting, limit=limit)
    assert keywords.analyze(posting, corpus, limit=limit) == reference_analyze(
        posting, corpus, limit=limit
    )


@pytest.mark.parametrize(
    ("posting", "expected"),
    [
        ("PostgreSQL Kafka postgresql KAFKA Go C# ML Python", ["PostgreSQL", "Kafka", "Python"]),
        (
            "machine Learning. Data pipelines. Data pipeline. Software",
            ["machine learning", "data pipelines", "Software"],
        ),
        (
            "micro-machine learning Rust/Tauri Redis",
            ["machine learning", "micro-machine", "Rust/Tauri", "Redis"],
        ),
        ("", []),
    ],
)
def test_job_terms_keep_surface_ties_and_phrase_boundaries(
    posting: str, expected: list[str]
) -> None:
    assert reference_job_terms(posting) == expected
    assert keywords._known_phrases(posting) == reference_known_phrases(posting)
    assert keywords.job_terms(posting) == expected


def test_returned_terms_are_independent() -> None:
    terms = keywords.job_terms(JD)
    expected = terms[:]
    terms.append("unrelated")
    assert keywords.job_terms(JD) == expected


def test_project_requirements_are_parsed_once_and_return_fresh_dicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str | None] = []
    extract = project_rank.extract

    def track(job_text: str | None) -> PostingRequirements:
        calls.append(job_text)
        return extract(job_text)

    posting = "Required Qualifications:\n- Experience with PostgreSQL and Docker."
    project_rank._required_skills.cache_clear()
    monkeypatch.setattr(project_rank, "extract", track)
    result = project_rank.required_skills(posting)
    assert result
    expected = result.copy()
    result.clear()
    assert project_rank.required_skills(posting) == expected
    assert calls == [posting]
