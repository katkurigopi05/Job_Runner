"""Hold optimized experience readings and stored chunk offsets to the old loops."""

from __future__ import annotations

import pytest

from packages.core.models import Posting
from packages.matching import chunks, experience, rubric, score
from packages.matching.eligibility import Evidence
from packages.matching.embed import LexicalEmbedder
from packages.matching.experience import Demand, PostingExperience, YearsDemand
from tests.test_experience_requirements import _golden, _posting, _profile


def reference_heading_kind(line: str) -> Demand | None:
    stripped = line.strip().rstrip(":").strip()
    if not stripped or len(stripped.split()) > experience._MAX_HEADING_WORDS:
        return None
    if stripped.endswith((".", ",", ";", "!", "?")):
        return None
    if any(c in ",;()[]0123456789•·▪|" for c in stripped):
        return None
    if experience._PREFERRED_HEADING.match(stripped):
        return Demand.PREFERRED
    if experience._MANDATORY_HEADING.match(stripped):
        return Demand.MANDATORY
    return Demand.AMBIGUOUS


def reference_line_demand(line: str, heading: Demand) -> Demand:
    if experience._LINE_PREFERRED.search(line):
        return Demand.PREFERRED
    if experience._LINE_MANDATORY.search(line):
        return Demand.MANDATORY
    return heading


def reference_read_posting(text: str | None) -> PostingExperience:
    if not text:
        return PostingExperience()
    heading = Demand.AMBIGUOUS
    demands: list[YearsDemand] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        kind = reference_heading_kind(line)
        if kind is not None:
            heading = kind
            continue
        for match in experience._YEARS.finditer(line):
            if not experience._is_demand(line, match):
                continue
            demands.append(
                YearsDemand(
                    years=int(match.group("years")),
                    demand=reference_line_demand(line, heading),
                    evidence=Evidence.of("experience", line),
                )
            )
    return PostingExperience(demands=tuple(demands))


def reference_spans(body: str) -> list[tuple[int, int]]:
    if not body:
        return []
    out: list[tuple[int, int]] = []
    start = 0
    while True:
        out.append((start, min(500, len(body) - start)))
        if start + 500 >= len(body):
            return out
        start += 375


POSTINGS = [pytest.param(text, id=title) for title, text in _golden().items()]


@pytest.mark.parametrize(
    "text",
    [
        *POSTINGS,
        None,
        "",
        "A Year at Palantir\n\nCome join for a year.",
        "Minimum Qualifications:\n8–12+ years of experience\nBenefits:\n4+ yrs of experience",
        (
            "Minimum Qualifications:\n• ٣—٥+ YRſ of experience\u2028"
            "Preferred:\n• ２\u00a0YEAR’S expertise"
        ),
        "What We Require:\n• ٣ years of experience\nBenefits (US):\n２ yrs of experience",
        "\u202fPréfaced heading:\r\n \u00a0" + "界🙂 résumé\t" * 130,
        "!!!\nEssential:\n- 2 yrs with Rust.\nRequirements:\n- 7 years preferred",
    ],
)
def test_readers_match_reference_on_complete_postings(text: str | None) -> None:
    assert experience.read_posting(text) == reference_read_posting(text)
    for line in (text or "").splitlines():
        assert experience.heading_kind(line) == reference_heading_kind(line)
        for heading in Demand:
            assert experience.line_demand(line, heading) == reference_line_demand(line, heading)
    body = " ".join((text or "").split())
    assert chunks.normalize(text or "") == body
    assert chunks.spans(body) == reference_spans(body)


@pytest.mark.parametrize("max_years", [2, 8])
def test_score_and_rubric_share_the_experience_read(
    max_years: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    posting = _posting("Minimum requirements:\n5+ years of experience with Python.")
    profile = _profile(max_required_experience_years=max_years)
    embedder = LexicalEmbedder()
    vector = embedder.encode(["Python"])[0]
    expected = score.score_posting(posting, profile, vector, embedder)
    reads = 0

    def read(text: str | None) -> PostingExperience:
        nonlocal reads
        reads += 1
        return experience.read_posting(text)

    def read_posting(posting: Posting) -> PostingExperience:
        return read(posting.description_raw)

    monkeypatch.setattr(score, "experience_of", read_posting)
    monkeypatch.setattr(rubric, "read_experience", read)
    assert score.score_posting(posting, profile, vector, embedder) == expected
    assert reads == 1
