"""Pure, offline proof of the pilot's separation, budget, and frozen protocol."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from apps.api.chat_selection import EvidenceItem
from packages.llm.answer_harness import (
    GUIDANCE_POLICIES,
    MAX_GUIDANCE,
    BudgetedCaller,
    FrozenSelectionCase,
    PilotBudgetError,
    PilotSplit,
    SelectionRun,
    capture_selection,
    digest,
    make_split,
    run_pilot,
    selection_prompt,
    validate_baseline,
)


class FakeProvider:
    name = "fake"

    def __init__(self, *, response=None, delay=0, guidance_key="all_constraints"):
        self.response = response
        self.delay = delay
        self.guidance_key = guidance_key
        self.calls = []

    async def complete(self, system, user, *, max_tokens, temperature):
        self.calls.append((system, user, max_tokens, temperature))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.response is not None:
            return self.response
        if max_tokens == 256:
            return json.dumps({"guidance": self.guidance_key})
        return '{"ids":["E1"]}'


def cases(count=15):
    return tuple(
        FrozenSelectionCase(
            id=f"case-{index}",
            question=f"QUESTION_SECRET_{index}",
            system="FIXED SYSTEM",
            prompt=f'EXACT_FROZEN_PROMPT_{index}\n{{"E1":"EXACT_SOURCE_SECRET_{index}"}}',
            evidence=(
                EvidenceItem(id="E1", text=f"EXACT_SOURCE_SECRET_{index}", source_label="P1"),
            ),
        )
        for index in range(count)
    )


def baseline_for(selected):
    return [
        SelectionRun(
            case.id,
            "baseline",
            repeat,
            '{"ids":["E1"]}',
            ("E1",),
            True,
            None,
            0.01,
            digest(case.system),
            digest(case.prompt),
        )
        for case in selected
        for repeat in range(1, 4)
    ]


def test_split_is_reproducible_order_independent_disjoint_and_reserved():
    selected = cases()
    split = make_split(selected)
    assert split == make_split(tuple(reversed(selected)))
    assert split != make_split(selected, seed=41)
    assert (len(split.development), len(split.holdout), len(split.reserved)) == (6, 6, 3)
    assert set(split.development).isdisjoint(split.holdout)
    assert set(split.reserved).isdisjoint(split.development + split.holdout)
    assert set(split.development + split.holdout + split.reserved) == {case.id for case in selected}


@pytest.mark.parametrize("kwargs", [{"development": 0}, {"holdout": -1}, {"seed": True}])
def test_split_rejects_invalid_limits(kwargs):
    with pytest.raises(ValueError):
        make_split(cases(), **kwargs)


def test_split_rejects_insufficient_and_duplicate_corpus():
    with pytest.raises(ValueError):
        make_split(cases(11))
    with pytest.raises(ValueError):
        make_split(cases() + (cases()[0],))


async def test_capture_preserves_system_and_catalog_and_uses_runtime_parser():
    case = cases(1)[0]
    provider = FakeProvider(response='{"ids":["invented"]}')
    events = []
    caller = BudgetedCaller(provider, max_calls=1, max_seconds=10, emit=events.append)
    guidance = GUIDANCE_POLICIES["direct_evidence"]
    run = await capture_selection(case, caller, guidance=guidance)
    assert provider.calls == [
        (
            case.system,
            case.prompt + "\n\nExperimental selection guidance:\n" + guidance,
            180,
            0,
        )
    ]
    assert case.evidence[0].text == "EXACT_SOURCE_SECRET_0"
    assert run.valid is False and run.ids == () and run.failure == "unknown_id"
    assert run.system_sha256 == digest(case.system)
    assert run.prompt_sha256 == digest(provider.calls[0][1])
    request = events[0]
    assert request["kind"] == "request" and request["attempt"] == 1
    assert "EXACT" not in json.dumps(request) and "FIXED" not in json.dumps(request)
    assert events[-1]["kind"] == "selection"
    assert events[-1]["run"]["raw"] == '{"ids":["invented"]}'


@pytest.mark.parametrize("guidance_key", GUIDANCE_POLICIES)
async def test_full_pilot_uses_76_requests_and_never_leaks_holdout_to_improver(guidance_key):
    selected = cases()
    split = make_split(selected)
    provider = FakeProvider(guidance_key=guidance_key)
    caller = BudgetedCaller(provider, max_calls=76, max_seconds=30)
    result = await run_pilot(selected, split, baseline_for(selected), caller)
    assert caller.calls == len(provider.calls) == 76
    assert len(result["runs"]) == 72
    assert result["chosen"] == "baseline" and result["promotion"] == "requires_owner_review"
    assert result["lesson"]["status"] == "unreviewed"
    advice = GUIDANCE_POLICIES[guidance_key]
    assert set(result["guidance"].values()) == {advice}
    assert result["lesson"]["guidance"] == advice
    assert len(advice) <= MAX_GUIDANCE
    selections = [user for _, user, tokens, _ in provider.calls if tokens == 180]
    assert all(
        prompt.endswith("\n\nExperimental selection guidance:\n" + advice) for prompt in selections
    )
    improver_prompts = [user for _, user, tokens, _ in provider.calls if tokens == 256]
    assert len(improver_prompts) == 4
    for case in selected:
        if case.id in split.holdout + split.reserved:
            for prompt in improver_prompts:
                assert case.question not in prompt
                assert case.prompt not in prompt
                assert case.evidence[0].text not in prompt
    for prompt in improver_prompts[:2]:
        payload = json.loads(prompt)
        example = payload["representative_development_case"]
        original = next(case for case in selected if case.question == example["question"])
        assert original.id in split.development
        assert example["frozen_prompt"] == original.prompt
        assert original.evidence[0].text in example["frozen_prompt"]
        assert "evidence" not in example
        assert len(payload["representative_baseline_selections"]) == 3
        assert all(row["ids"] == ["E1"] for row in payload["representative_baseline_selections"])
    composed = json.loads(improver_prompts[2])
    reflected = json.loads(improver_prompts[3])
    assert len(composed["representative_selections"]) == 9
    assert len(reflected["representative_selections"]) == 6
    assert {row["stage"] for row in reflected["representative_selections"]} == {
        "baseline",
        "development_composed",
    }
    assert all(system == "FIXED SYSTEM" for system, _, tokens, _ in provider.calls if tokens == 180)
    assert all(temperature == 0 for _, _, tokens, temperature in provider.calls if tokens == 180)
    holdout_positions = [
        index
        for index, (_, prompt, _, _) in enumerate(provider.calls)
        if any(case.prompt in prompt for case in selected if case.id in split.holdout)
    ]
    assert min(holdout_positions) > 55  # Both proposals and composition preceded holdout.
    assert "semantic accuracy" in result["metric_limit"]


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '{"guidance":"x","answer":"invented"}',
        '{"guidance":""}',
        '{"guidance":true}',
        '{"guidance":"x","guidance":"y"}',
        '{"guidance":"all_constraints "}',
        json.dumps({"guidance": GUIDANCE_POLICIES["all_constraints"]}),
        json.dumps({"guidance": "x" * (MAX_GUIDANCE + 1)}),
        json.dumps(
            {
                "guidance": "Select evidence that explicitly mentions Kafka and low-latency "
                "streaming pipelines, prioritizing Kafka-specific requirements."
            }
        ),
    ],
)
async def test_malformed_improvement_aborts_without_probe(raw):
    selected = cases()
    provider = FakeProvider(response=raw)
    caller = BudgetedCaller(provider, max_calls=76, max_seconds=10)
    with pytest.raises(ValueError):
        await run_pilot(selected, make_split(selected), baseline_for(selected), caller)
    assert caller.calls == len(provider.calls) == 1


@pytest.mark.parametrize(
    "guidance", ["all_constraints", "Focus on Kafka and low-latency pipelines."]
)
async def test_capture_rejects_unapproved_suffix_before_provider_dispatch(guidance):
    provider = FakeProvider()
    caller = BudgetedCaller(provider, max_calls=1, max_seconds=10)
    with pytest.raises(ValueError, match="fixed selection policy"):
        await capture_selection(cases(1)[0], caller, guidance=guidance)
    assert caller.calls == 0 and not provider.calls


def test_baseline_prompt_has_no_experimental_suffix():
    case = cases(1)[0]
    assert selection_prompt(case) == case.prompt


@pytest.mark.parametrize(
    "mutation",
    [
        lambda rows: rows[:-1],
        lambda rows: rows + [rows[0]],
        lambda rows: [replace(rows[0], stage="candidate"), *rows[1:]],
        lambda rows: [replace(rows[0], case_id="unknown"), *rows[1:]],
        lambda rows: [replace(rows[0], prompt_sha256=digest("changed")), *rows[1:]],
        lambda rows: [replace(rows[0], system_sha256=digest("changed")), *rows[1:]],
        lambda rows: [replace(rows[0], ids=("invented",)), *rows[1:]],
        lambda rows: [replace(rows[0], raw='{"ids":["invented"]}'), *rows[1:]],
        lambda rows: [replace(rows[0], valid=False), *rows[1:]],
        lambda rows: [replace(rows[0], repeat=True), *rows[1:]],
    ],
)
def test_baseline_requires_complete_exact_untampered_observations(mutation):
    selected = cases()
    with pytest.raises(ValueError):
        validate_baseline(selected, mutation(baseline_for(selected)))


async def test_invalid_split_aborts_without_call():
    selected = cases()
    split = make_split(selected)
    bad = PilotSplit(split.development, split.development, split.reserved, split.seed)
    provider = FakeProvider()
    caller = BudgetedCaller(provider, max_calls=76, max_seconds=10)
    with pytest.raises(ValueError):
        await run_pilot(selected, bad, baseline_for(selected), caller)
    assert caller.calls == 0


async def test_budget_counts_failed_attempt_before_await_and_never_retries():
    class FailingProvider(FakeProvider):
        async def complete(self, *args, **kwargs):
            raise RuntimeError("private error should be sanitized by CLI")

    caller = BudgetedCaller(FailingProvider(), max_calls=1, max_seconds=10)
    with pytest.raises(RuntimeError):
        await caller.complete("system", "user", stage="baseline", max_tokens=180, temperature=0)
    assert caller.calls == 1
    with pytest.raises(PilotBudgetError):
        await caller.complete("system", "user", stage="baseline", max_tokens=180, temperature=0)
    assert caller.calls == 1


async def test_call_budget_stops_partial_pilot_at_exact_limit():
    selected = cases()
    provider = FakeProvider()
    caller = BudgetedCaller(provider, max_calls=75, max_seconds=10)
    with pytest.raises(PilotBudgetError):
        await run_pilot(selected, make_split(selected), baseline_for(selected), caller)
    assert caller.calls == len(provider.calls) == 75


async def test_wall_budget_includes_slow_inflight_request():
    provider = FakeProvider(delay=1)
    caller = BudgetedCaller(provider, max_calls=2, max_seconds=0.02)
    with pytest.raises(PilotBudgetError, match="wall-time"):
        await caller.complete("system", "user", stage="baseline", max_tokens=180, temperature=0)
    assert caller.calls == len(provider.calls) == 1
    with pytest.raises(PilotBudgetError):
        await caller.complete("system", "user", stage="baseline", max_tokens=180, temperature=0)
    assert caller.calls == 1


async def test_wall_budget_includes_time_before_first_request():
    caller = BudgetedCaller(FakeProvider(), max_calls=2, max_seconds=0.01)
    await asyncio.sleep(0.02)
    with pytest.raises(PilotBudgetError):
        await caller.complete("system", "user", stage="baseline", max_tokens=180, temperature=0)
    assert caller.calls == 0
