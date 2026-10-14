"""`dispatch.tailor`, the one function apply, the batch and Compare models share.

What is held here, with a stub for the model:

- Either mode returns one entry per bullet.
- A targeted run with nothing to gain never calls the provider factory, and
  says it made no call.
- A revision that adds a supported term beside its evidence is kept.
- One that drops a figure or adds an unsupported skill is refused and the
  bullet is the résumé's own.
- One repair at most, then the original bullet.
- `plan_needed_calls`, which the batch budgets its allowance with.

The callers themselves, the cache and the PDF check are in
`tests/test_tailor_mode_callers.py`. The rules a revision is judged by are in
`tests/test_targeted_tailoring.py` and `tests/test_practice_evidence.py`.
"""

from __future__ import annotations

import pytest

from packages.llm.provider import StubProvider
from packages.tailor.dispatch import HEURISTIC_NOTICE, plan_needed_calls, tailor
from packages.tailor.parse import ParsedResume

CLASSIFIER = "Built a classification pipeline over 148 years of fire data, reaching ROC-AUC 0.72."
LOADER = "Loaded 1,000 billing records with pytest checks."
HANDBOOK = "Wrote the on-call handbook for the support rotation."
SKILLS = "Languages & Tools  Python, pytest, pandas"

POSTING_MATCH = "Requirements:\nClassification, automated testing, and Kubernetes.\n"
POSTING_NO_GAIN = "Requirements:\nClassification, Python, and pandas.\n"

ACCEPTED = "Loaded 1,000 billing records with automated testing via pytest checks."


def _resume(experience: list[str], **sections: list[str]) -> ParsedResume:
    body = {"experience": experience, **sections}
    raw = [line for name, lines in body.items() for line in (name.title(), *lines)]
    return ParsedResume.model_validate(
        {
            "contact": {"name": "Test Candidate", "email": "candidate@example.com", "links": []},
            "preamble": [],
            "sections": body,
            "raw_lines": raw,
        }
    )


class ScriptedProvider(StubProvider):
    def __init__(self, *answers: str) -> None:
        super().__init__()
        self._answers = list(answers)

    async def complete(self, system, user, *, max_tokens=1024, temperature=0.2) -> str:
        self.calls.append((system, user))
        return self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]


@pytest.mark.asyncio
async def test_zero_opportunities_makes_zero_provider_calls() -> None:
    """When the résumé already covers or cannot truthfully support any new posting term,
    the provider factory must NOT be invoked at all.
    """
    parsed = _resume([CLASSIFIER, HANDBOOK], skills=[SKILLS])
    factory_called = False

    def factory() -> StubProvider:
        nonlocal factory_called
        factory_called = True
        return StubProvider()

    tailored = await tailor(
        factory,
        parsed,
        POSTING_NO_GAIN,
        mode="targeted",
    )

    assert not factory_called, "Provider factory was called when there was zero opportunity!"
    assert tailored.no_gain and tailored.calls == 0
    assert tailored.message == "no supported keyword gain available"
    assert tailored.gained == ()
    assert len(tailored.result.bullets) == 2
    # All bullets remain unchanged
    assert all(b.tailored == b.original for b in tailored.result.bullets)


@pytest.mark.asyncio
async def test_rewrite_mode_returns_one_entry_per_bullet() -> None:
    """Rewrite mode rewrites bullets through tailor_bullets and returns one entry per bullet."""
    parsed = _resume([CLASSIFIER, LOADER, HANDBOOK])
    provider = StubProvider({"classification": CLASSIFIER, "billing": LOADER, "handbook": HANDBOOK})

    tailored = await tailor(
        lambda: provider,
        parsed,
        POSTING_MATCH,
        mode="rewrite",
    )

    assert tailored.mode == "rewrite"
    assert len(tailored.result.bullets) == 3
    assert not tailored.no_gain
    # One call a bullet, which is what the batch counts against its allowance.
    assert tailored.calls == len(provider.calls) == 3


@pytest.mark.asyncio
async def test_genuinely_supported_keyword_survives() -> None:
    """A keyword genuinely supported by practices (e.g. automated testing backed by pytest)
    is accepted when rewritten truthfully.
    """
    parsed = _resume([CLASSIFIER, LOADER, HANDBOOK], skills=[SKILLS])
    provider = ScriptedProvider(ACCEPTED)

    tailored = await tailor(
        lambda: provider,
        parsed,
        POSTING_MATCH,
        mode="targeted",
    )

    assert "automated testing" in tailored.gained
    rewritten = [b for b in tailored.result.bullets if b.tailored == ACCEPTED]
    assert len(rewritten) == 1
    assert rewritten[0].rejected_reason is None


@pytest.mark.asyncio
async def test_figures_metrics_and_model_names_survive() -> None:
    """Numbers like 0.72, 1,000, units, and technical terms survive rewriting."""
    parsed = _resume([CLASSIFIER, LOADER])
    # Model drops 1,000 -> must be rejected and fallback to original
    dropped_count = "Loaded billing records with automated testing via pytest checks."
    provider = ScriptedProvider(dropped_count, dropped_count)

    tailored = await tailor(
        lambda: provider,
        parsed,
        POSTING_MATCH,
        mode="targeted",
    )

    # The loader bullet should have fallen back to original because 1,000 was dropped
    loader_bullet = [b for b in tailored.result.bullets if "1,000" in b.original][0]
    assert loader_bullet.tailored == loader_bullet.original
    assert loader_bullet.rejected_reason is not None
    assert "drops the figure" in loader_bullet.rejected_reason


@pytest.mark.asyncio
async def test_invented_gap_skills_are_rejected() -> None:
    """Adding skills not present in the résumé (such as Kubernetes when unbacked) is rejected."""
    parsed = _resume([CLASSIFIER, LOADER])
    hallucinated = (
        "Loaded 1,000 billing records with automated testing via pytest on Kubernetes cluster."
    )
    provider = ScriptedProvider(hallucinated, hallucinated)

    tailored = await tailor(
        lambda: provider,
        parsed,
        POSTING_MATCH,
        mode="targeted",
    )

    # Kubernetes is an unsupported gap; rewrite introducing it must be rejected
    loader_bullet = [b for b in tailored.result.bullets if "1,000" in b.original][0]
    assert loader_bullet.tailored == loader_bullet.original
    assert loader_bullet.rejected_reason is not None


@pytest.mark.asyncio
async def test_one_repair_maximum_then_original_fallback() -> None:
    """Targeted rewriter attempts one repair maximum if first attempt drops a figure or adds a gap,
    and falls back to original bullet if second attempt also fails.
    """
    parsed = _resume([CLASSIFIER, LOADER], skills=[SKILLS])
    bad_attempt_1 = "Loaded billing records with automated testing."  # dropped 1,000 and pytest
    bad_attempt_2 = (
        "Loaded billing records with automated testing and pytest."  # still dropped 1,000
    )
    provider = ScriptedProvider(bad_attempt_1, bad_attempt_2)

    tailored = await tailor(
        lambda: provider,
        parsed,
        POSTING_MATCH,
        mode="targeted",
    )

    # Should have called provider exactly 2 times (initial + 1 repair), then given up
    assert len(provider.calls) == 2
    loader_bullet = [b for b in tailored.result.bullets if "1,000" in b.original][0]
    assert loader_bullet.tailored == loader_bullet.original


@pytest.mark.asyncio
async def test_successful_repair_is_accepted() -> None:
    """If the first attempt drops a figure, but the repair succeeds,
    the repair is accepted.
    """
    parsed = _resume([CLASSIFIER, LOADER], skills=[SKILLS])
    bad_attempt_1 = (
        "Loaded billing records with automated testing via pytest checks."  # dropped 1,000
    )
    good_attempt_2 = ACCEPTED  # repaired with 1,000
    provider = ScriptedProvider(bad_attempt_1, good_attempt_2)

    tailored = await tailor(
        lambda: provider,
        parsed,
        POSTING_MATCH,
        mode="targeted",
    )

    assert len(provider.calls) == 2
    loader_bullet = [b for b in tailored.result.bullets if "1,000" in b.original][0]
    assert loader_bullet.tailored == ACCEPTED
    assert "automated testing" in tailored.gained


def test_plan_needed_calls_budgets_worst_case_correctly() -> None:
    """Worst-case call budgeting:
    - rewrite: len(bullets)
    - targeted with no targets: 0 calls
    - targeted with 1 target: 2 calls (initial + 1 repair)
    """
    parsed = _resume([CLASSIFIER, LOADER, HANDBOOK], skills=[SKILLS])

    # In rewrite mode: 3 calls
    calls_rewrite = plan_needed_calls(parsed, POSTING_MATCH, mode="rewrite")
    assert calls_rewrite == 3

    # In targeted mode with 1 target ("automated testing"): 2 calls (1 target * 2)
    calls_targeted = plan_needed_calls(parsed, POSTING_MATCH, mode="targeted")
    assert calls_targeted == 2

    # In targeted mode with no gain: 0 calls
    calls_no_gain = plan_needed_calls(parsed, POSTING_NO_GAIN, mode="targeted")
    assert calls_no_gain == 0


def test_heuristic_notice_contains_disclaimer() -> None:
    """Heuristic notice clearly disclaims predictive power on hiring success."""
    assert "not predictions of hiring success" in HEURISTIC_NOTICE.lower()
