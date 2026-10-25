"""Synthetic bullets only: actions cannot gain completion or responsibility."""

import pytest

from packages.llm.provider import StubProvider
from packages.tailor.guard import SourceCorpus
from packages.tailor.parse import ParsedResume
from packages.tailor.rewrite import tailor_bullet, vet


@pytest.mark.parametrize("source", ["Designing", "Building", "Exploring"])
@pytest.mark.parametrize("target", ["Created", "Built", "Shipped", "Launched", "Delivered"])
def test_in_progress_cannot_become_finished(source: str, target: str) -> None:
    original = f"{source} a search engine in Python."
    accepted, reason, _ = vet(
        original, f"{target} a search engine in Python.", SourceCorpus.from_texts(original)
    )
    assert not accepted
    assert reason and "claim strength" in reason and source.lower() in reason


@pytest.mark.parametrize("source", ["Designing", "Building", "Exploring"])
@pytest.mark.parametrize("target", ["Developed", "Implemented", "Completed", "Deployed"])
def test_in_progress_cannot_become_developed_or_deployed(source: str, target: str) -> None:
    # Added 2026-10-25. A model turned "Designing" into "Developed" and only a
    # borrowed posting word stopped it. The verbs are in the corpus here, so
    # nothing but the claim-strength check can refuse the rewrite.
    original = f"{source} a search engine in Python."
    accepted, reason, _ = vet(
        original,
        f"{target} a search engine in Python.",
        SourceCorpus.from_texts(original, "Developed Implemented Completed Deployed"),
    )
    assert not accepted
    assert reason and "claim strength" in reason and source.lower() in reason


@pytest.mark.parametrize("source", ["Contributed to", "Helped", "Assisted"])
@pytest.mark.parametrize("target", ["Led", "Owned", "Managed", "Architected"])
def test_participation_cannot_become_ownership(source: str, target: str) -> None:
    original = f"{source} the search engine project."
    # These verbs elsewhere already satisfy the entity/scope-token check;
    # they must still not strengthen this particular source bullet.
    accepted, reason, _ = vet(
        original,
        f"{target} the search engine project.",
        SourceCorpus.from_texts(original, "Led Owned Managed Architected"),
    )
    assert not accepted
    assert reason and "claim strength" in reason


@pytest.mark.parametrize(
    ("original", "candidate"),
    [
        ("Designing a search engine in Python.", "Building a search engine in Python."),
        ("Built a search engine in Python.", "Building a search engine in Python."),
        ("Led the search engine project.", "Helped the search engine project."),
        ("Helped the search engine project.", "Assisted the search engine project."),
        ("Building a search engine in Python.", "Building a search engine using Python."),
        ("Designing a search engine in Python.", "Helped build a search engine in Python."),
        (
            "Helped the search engine project.",
            "Helped the search engine project, managed by a colleague.",
        ),
        ("Exploring a building search engine.", "Exploring a building search engine."),
    ],
)
def test_same_or_lower_claim_passes(original: str, candidate: str) -> None:
    accepted, reason, _ = vet(original, candidate, SourceCorpus.from_texts(original, candidate))
    assert accepted, reason


def test_another_bullet_cannot_license_completion() -> None:
    original = "- Currently designing a search engine in Python."
    lines = ["Example Project", original, "- Created a test harness in Python."]
    corpus = SourceCorpus.from_resume(ParsedResume(sections={"projects": lines}, raw_lines=lines))
    assert not vet(original, "- Created a search engine in Python.", corpus)[0]


async def test_upgrade_falls_back_to_original() -> None:
    original = "Designing a search engine in Python."
    result = await tailor_bullet(
        StubProvider({"Original bullet:": "Created a search engine in Python."}),
        original,
        "Python engineer",
        SourceCorpus.from_texts(original),
    )
    assert result.tailored == original
    assert result.guard_refused
    assert "claim strength" in (result.rejected_reason or "")


def test_coordinated_upgrade_is_refused() -> None:
    original = "Designing a search engine in Python."
    accepted, reason, _ = vet(
        original,
        "Designing a search engine in Python; shipped it.",
        SourceCorpus.from_texts(original),
    )
    assert not accepted
    assert reason and "claim strength" in reason
