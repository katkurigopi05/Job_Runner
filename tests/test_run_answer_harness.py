"""Offline CLI integration: private files, immutable plans and guarded execution."""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from pydantic import TypeAdapter

from apps.api.answer_support import AnswerReview
from apps.api.routers import chat
from packages.core.schemas import ChatReply
from packages.llm.answer_harness import selection_prompt
from packages.llm.kaggle import KaggleStatus
from packages.matching.retrieve import Passage, Retrieval
from scripts import run_answer_harness as cli


class FakeProvider:
    name = "kaggle"

    def __init__(self, *, fail=False):
        self.calls = []
        self.fail = fail

    async def complete(self, system, user, *, max_tokens, temperature):
        self.calls.append((system, user, max_tokens, temperature))
        if self.fail:
            raise RuntimeError("PRIVATE_PROMPT_CREDENTIAL_MUST_NOT_ESCAPE")
        if max_tokens == 256:
            return '{"guidance":"all_constraints"}'
        if user == "PROSE_WITH_MAIL_EXCLUDED":
            return "Recruiter mail was excluded."
        return '{"ids":["P1"]}'


def frozen_cases():
    found = Retrieval(
        attempted=True,
        searched=1,
        passages=(
            Passage(
                "P1",
                UUID("00000000-0000-0000-0000-000000000001"),
                "Engineer",
                "Example",
                "San Francisco, CA",
                "https://example.test/job",
                "Source requires Python.",
                None,
            ),
        ),
    )
    dumped = TypeAdapter(Retrieval).dump_python(found, mode="json")
    rows = [
        {
            "id": f"selection-{index}",
            "mode": "selection",
            "system": chat.SELECTION_SYSTEM,
            "question": f"Question {index}?",
            "prompt": 'EVIDENCE_SELECTION\n{"id":"P1","text":"Source requires Python."}'
            f"\nQuestion {index}?",
            "evidence": [{"id": "P1", "text": "Source requires Python.", "source_label": "P1"}],
            "found": dumped,
        }
        for index in range(12)
    ]
    rows.append(
        {
            "id": "facts",
            "mode": "facts",
            "system": chat.SYSTEM,
            "question": "How many?",
            "prompt": "DETERMINISTIC_FACTS",
            "evidence": [],
            "found": dumped,
            "code_reply": ChatReply(
                reply="One matching posting.",
                provider="facts",
                grounded=True,
                shared_mail=False,
                local=True,
            ).model_dump(mode="json"),
        }
    )
    rows.append(
        {
            "id": "prose",
            "mode": "prose",
            "system": chat.SYSTEM,
            "question": "Recruiter mail?",
            "prompt": "PROSE_WITH_MAIL_EXCLUDED",
            "evidence": [],
            "found": TypeAdapter(Retrieval).dump_python(Retrieval(attempted=False), mode="json"),
        }
    )
    return rows


@pytest.fixture
def storage(tmp_path, monkeypatch):
    root = tmp_path / "experiment-storage"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(storage_root=root))
    return root


def prepared(storage):
    source = storage / "source.json"
    cli.write_private(source, frozen_cases())
    run = storage / "pilot"
    plan = cli.prepare(source, run)
    return run, plan


def fake_live(monkeypatch, provider):
    @asynccontextmanager
    async def live(expected_model=cli.MODEL):
        yield provider

    monkeypatch.setattr(cli, "live_provider", live)


def test_prepare_freezes_private_disjoint_plan(storage):
    run, plan = prepared(storage)
    assert os.stat(run).st_mode & 0o777 == 0o700
    assert all(os.stat(run / name).st_mode & 0o777 == 0o600 for name in ("plan.json", "cases.json"))
    assert len(plan["split"]["development"]) == len(plan["split"]["holdout"]) == 6
    assert set(plan["split"]["development"]).isdisjoint(plan["split"]["holdout"])
    assert plan["baseline_max_calls"] == 37
    assert plan["pilot_max_calls"] == 76
    assert plan["share_mail"] is False
    assert plan["promotion"] == "requires_owner_review"
    _, loaded, _ = cli.load_plan(run)
    assert loaded == json.loads(json.dumps(plan))


@pytest.mark.parametrize("target", ["cases.json", "plan.json"])
def test_changed_cases_or_plan_refuse_before_provider(storage, target):
    run, _ = prepared(storage)
    value = cli.read_private(run / target)
    if target == "cases.json":
        value[0]["prompt"] += "CHANGED"
    else:
        value["baseline_max_calls"] += 1
    cli.write_private(run / target, value)
    with pytest.raises(ValueError):
        cli.load_plan(run)


def test_outside_and_symlink_paths_refuse(storage, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text("[]")
    outside.chmod(0o600)
    with pytest.raises(ValueError):
        cli.read_private(outside)
    with pytest.raises(ValueError):
        cli.write_private(outside, [])
    private = storage / "private.json"
    cli.write_private(private, [])
    link = storage / "symlink.json"
    link.symlink_to(private)
    with pytest.raises(ValueError):
        cli.read_private(link)
    with pytest.raises(ValueError):
        cli.write_private(link, [])
    assert private.read_text().strip() == "[]"


def test_nonprivate_input_and_nonregular_input_refuse(storage):
    source = storage / "public.json"
    source.write_text("[]")
    source.chmod(0o644)
    with pytest.raises(ValueError, match="owner-private"):
        cli.read_private(source)
    # Opening a directory works at the descriptor layer; regular-file check rejects it.
    with pytest.raises((ValueError, IsADirectoryError)):
        cli.read_private(storage)


async def test_not_ready_never_constructs_provider_or_database(storage, monkeypatch):
    run, _ = prepared(storage)
    monkeypatch.setattr(
        cli.kaggle_connection,
        "saved_status",
        AsyncMock(return_value=KaggleStatus(state="unreachable", detail="Session unavailable.")),
    )

    def forbidden(*args, **kwargs):
        pytest.fail("not-ready path must not construct provider or DB")

    monkeypatch.setattr(cli.chat, "provider_for", forbidden)
    monkeypatch.setattr(cli, "create_async_engine", forbidden)
    state = await cli.run_phase(run, "baseline")
    assert state["status"] == "blocked_provider"
    assert state["readiness"] == "unreachable"
    assert state["model_calls"] == 0
    assert not state["selection_runs"] and not state["responses"]


async def test_baseline_captures_every_mode_and_completed_rerun_is_offline(storage, monkeypatch):
    run, _ = prepared(storage)
    provider = FakeProvider()
    fake_live(monkeypatch, provider)
    state = await cli.run_phase(run, "baseline")
    assert state["status"] == "captured_ungraded"
    assert state["model_calls"] == len(provider.calls) == 37
    assert len(state["selection_runs"]) == 36
    assert len(state["responses"]) == 38
    assert state["responses"][-2]["reply"]["reply"] == "One matching posting."
    reviews = cli.read_private(run / "baseline_reviews.json")
    assert len(reviews) == 37
    assert all(AnswerReview.model_validate(review) for review in reviews)
    assert all(line["verdict"] == "pending" for review in reviews for line in review["lines"])

    async def forbidden():
        pytest.fail("completed phase must not reconstruct provider")

    monkeypatch.setattr(cli, "live_provider", forbidden)
    again = await cli.run_phase(run, "baseline")
    assert again["status"] == "captured_ungraded"
    assert len(provider.calls) == 37


@pytest.mark.parametrize(
    "mutation", ["missing", "incomplete", "different_plan", "tampered_selection"]
)
async def test_pilot_requires_complete_matching_baseline_before_provider(
    storage, monkeypatch, mutation
):
    run, plan = prepared(storage)
    provider = FakeProvider()
    fake_live(monkeypatch, provider)
    state = await cli.run_phase(run, "baseline")
    if mutation == "missing":
        (run / "baseline.json").unlink()
    else:
        if mutation == "incomplete":
            state["responses"].pop()
        elif mutation == "different_plan":
            state["plan_sha256"] = "incorrect"
        else:
            state["selection_runs"][0]["ids"] = ["unknown"]
        cli.write_private(run / "baseline.json", state)

    async def forbidden():
        pytest.fail("invalid baseline must not construct provider")

    monkeypatch.setattr(cli, "live_provider", forbidden)
    with pytest.raises((ValueError, FileNotFoundError)):
        await cli.run_phase(run, "pilot")
    assert len(provider.calls) == plan["baseline_max_calls"]
    assert not (run / "pilot.json").exists()


async def test_exception_text_stays_out_of_state_and_public_summary(storage, monkeypatch, capsys):
    run, _ = prepared(storage)
    fake_live(monkeypatch, FakeProvider(fail=True))
    state = await cli.run_phase(run, "baseline")
    assert state["status"] == "failed" and state["error_type"] == "RuntimeError"
    assert state["model_calls"] == 1
    print(json.dumps(cli.summary(state)))
    assert "PRIVATE_PROMPT_CREDENTIAL" not in capsys.readouterr().out
    assert "PRIVATE_PROMPT_CREDENTIAL" not in (run / "baseline.json").read_text()
    assert "EVIDENCE_SELECTION" not in json.dumps(cli.summary(state))


async def test_phase_lock_refuses_second_writer_without_state_or_model(storage, monkeypatch):
    run, _ = prepared(storage)
    provider = FakeProvider()
    fake_live(monkeypatch, provider)
    with cli.phase_files(run, "baseline"):
        journals_before = set(run.iterdir())
        with pytest.raises(FileExistsError):
            await cli.run_phase(run, "baseline")
        assert set(run.iterdir()) == journals_before
        assert not (run / "baseline.json").exists()
    assert not (run / ".baseline.lock").exists()
    assert provider.calls == []


async def test_candidate_reviews_freeze_exact_sent_prompt_and_pending_owner_review(
    storage, monkeypatch
):
    run, _ = prepared(storage)
    provider = FakeProvider()
    fake_live(monkeypatch, provider)
    await cli.run_phase(run, "baseline")
    state = await cli.run_phase(run, "pilot")
    assert state["status"] == "captured_ungraded"
    assert state["model_calls"] == 76
    reviews = cli.read_private(run / "candidate_reviews.json")
    assert len(reviews) == 72
    actual_selection_prompts = [user for _, user, tokens, _ in provider.calls[37:] if tokens == 180]
    assert len(actual_selection_prompts) == len(reviews)
    assert [record["prompt"] for record in reviews] == actual_selection_prompts
    assert all(record["system"] == chat.SELECTION_SYSTEM for record in reviews)
    assert all(AnswerReview.model_validate(record) for record in reviews)
    assert all(
        line["verdict"] == "pending" and line["reviewer"] is None
        for record in reviews
        for line in record["lines"]
    )
    assert state["result"]["chosen"] == "baseline"
    assert state["result"]["promotion"] == "requires_owner_review"
    selected = cli.selection_cases(cli.read_private(run / "cases.json"))
    sample = selected[0]
    advice = state["result"]["guidance"]["composed"]
    assert selection_prompt(sample, advice) in actual_selection_prompts


async def test_blocked_retry_lock_refuses_archival_or_other_writes(storage, monkeypatch):
    run, plan = prepared(storage)
    cli.write_private(
        run / "baseline.json",
        {
            "plan_sha256": plan["plan_sha256"],
            "status": "blocked_provider",
            "model_calls": 0,
        },
    )
    provider = FakeProvider()
    fake_live(monkeypatch, provider)
    with cli.phase_files(run, "baseline"):
        files_before = {path.name: path.read_bytes() for path in run.iterdir()}
        with pytest.raises(FileExistsError):
            await cli.run_phase(run, "baseline")
        assert {path.name: path.read_bytes() for path in run.iterdir()} == files_before
    assert provider.calls == []


async def test_context_free_protected_refusal_never_reaches_model(storage, monkeypatch):
    rows = frozen_cases()
    rows.append(
        {
            "id": "protected-refusal",
            "mode": "facts",
            "question": "PRIVATE_PROTECTED_QUESTION",
            "code_reply": ChatReply(
                reply="Refused.", provider="refused", local=True, grounded=True
            ).model_dump(mode="json"),
        }
    )
    source = storage / "refusals.json"
    cli.write_private(source, rows)
    run = storage / "refusal-pilot"
    cli.prepare(source, run)
    provider = FakeProvider()
    fake_live(monkeypatch, provider)
    state = await cli.run_phase(run, "baseline")
    assert state["status"] == "captured_ungraded"
    assert state["model_calls"] == 37
    assert len(state["responses"]) == 39
    assert all(
        "PRIVATE_PROTECTED_QUESTION" not in system + user for system, user, _, _ in provider.calls
    )
    assert len(cli.read_private(run / "baseline_reviews.json")) == 37


async def test_ready_provider_scopes_readonly_privacy_database_and_restores_factory(
    storage, monkeypatch
):
    provider = FakeProvider()
    monkeypatch.setattr(
        cli.kaggle_connection,
        "saved_status",
        AsyncMock(return_value=KaggleStatus(state="ready", detail="Ready.", model=cli.MODEL)),
    )
    monkeypatch.setattr(cli.chat, "provider_for", lambda name: (provider, cli.MODEL))
    provider.health = AsyncMock(
        return_value=KaggleStatus(state="ready", detail="Ready.", model=cli.MODEL)
    )
    configuration = {}
    engine = SimpleNamespace(dispose=AsyncMock())

    def create_engine(url, **kwargs):
        configuration.update(url=url, **kwargs)
        return engine

    monkeypatch.setattr(cli, "create_async_engine", create_engine)
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(
            storage_root=storage, async_database_url="postgresql+asyncpg://offline.invalid/test"
        ),
    )
    factory = object()
    monkeypatch.setattr(cli, "async_sessionmaker", lambda *args, **kwargs: factory)
    previous = cli.db.get_sessionmaker
    async with cli.live_provider() as actual:
        assert actual is provider
        assert cli.db.get_sessionmaker() is factory
    assert cli.db.get_sessionmaker is previous
    assert configuration["connect_args"] == {
        "server_settings": {"default_transaction_read_only": "on"}
    }
    engine.dispose.assert_awaited_once()


def test_rehashed_plan_cannot_raise_budget_or_change_provider(storage):
    run, _ = prepared(storage)
    plan = cli.read_private(run / "plan.json")
    plan.pop("plan_sha256")
    plan["pilot_max_calls"] += 1
    plan["plan_sha256"] = cli.digest(cli._json(plan))
    cli.write_private(run / "plan.json", plan)
    with pytest.raises(ValueError, match="identity"):
        cli.load_plan(run)


def test_runtime_change_refuses_old_plan(storage, monkeypatch):
    run, _ = prepared(storage)
    monkeypatch.setattr(cli, "runtime_identity", lambda: "changed-runtime")
    with pytest.raises(ValueError, match="identity"):
        cli.load_plan(run)


async def test_selected_auto_model_is_frozen_and_used_in_response_labels(storage, monkeypatch):
    from packages.llm.kaggle import QWEN38_MODEL

    source = storage / "source.json"
    cli.write_private(source, frozen_cases())
    run = storage / "auto-model"
    plan = cli.prepare(source, run, model=QWEN38_MODEL)
    assert cli.load_plan(run)[1]["model"] == QWEN38_MODEL
    assert plan["model"] == QWEN38_MODEL
    fake_live(monkeypatch, FakeProvider())
    state = await cli.run_phase(run, "baseline")
    assert state["status"] == "captured_ungraded"
    assert {
        row["reply"]["model"] for row in state["responses"] if row["reply"]["provider"] != "facts"
    } == {QWEN38_MODEL}


async def test_full_phase_quota_is_required_before_any_provider_call(storage, monkeypatch):
    run, _ = prepared(storage)
    monkeypatch.setattr(cli, "remaining", lambda _: 1)
    forbidden = AsyncMock(side_effect=AssertionError("No provider may be constructed"))
    monkeypatch.setattr(cli, "live_provider", forbidden)
    state = await cli.run_phase(run, "baseline")
    assert state["status"] == "blocked_quota" and state["model_calls"] == 0
    forbidden.assert_not_called()
    monkeypatch.setattr(cli, "remaining", lambda _: 200)
    fake_live(monkeypatch, FakeProvider())
    assert (await cli.run_phase(run, "baseline"))["status"] == "captured_ungraded"


async def test_model_change_refuses_frozen_run_before_constructing_provider(storage, monkeypatch):
    from packages.llm.kaggle import QWEN38_MODEL

    run, _ = prepared(storage)
    monkeypatch.setattr(
        cli.kaggle_connection,
        "saved_status",
        AsyncMock(
            return_value=KaggleStatus(
                state="ready",
                detail="Ready",
                model=QWEN38_MODEL,
            )
        ),
    )
    forbidden = AsyncMock(side_effect=AssertionError("No provider may be constructed"))
    monkeypatch.setattr(cli.chat, "provider_for", forbidden)
    state = await cli.run_phase(run, "baseline")
    assert state["status"] == "blocked_provider" and state["model_calls"] == 0
    assert state["readiness"] == "model_not_ready"
    forbidden.assert_not_called()


def test_prepare_auto_model_option_resolves_before_freezing(storage, monkeypatch, capsys):
    import sys

    from packages.llm.kaggle import QWEN38_MODEL

    source, run = storage / "source.json", storage / "auto-option"
    cli.write_private(source, frozen_cases())
    monkeypatch.setattr(cli, "settings_env_file", lambda: storage / "settings.env")
    monkeypatch.setattr(
        cli.kaggle_connection,
        "saved_status",
        AsyncMock(
            return_value=KaggleStatus(
                state="ready",
                detail="Ready",
                model=QWEN38_MODEL,
            )
        ),
    )
    monkeypatch.setattr(
        sys, "argv", ["run_answer_harness", "prepare", str(source), str(run), "--auto-model"]
    )
    assert cli.main() == 0
    assert cli.read_private(run / "plan.json")["model"] == QWEN38_MODEL
    assert json.loads(capsys.readouterr().out)["model"] == QWEN38_MODEL


def test_nonowner_private_file_refused(storage, monkeypatch):
    source = storage / "owner.json"
    cli.write_private(source, [])
    original_uid = os.getuid()
    with monkeypatch.context() as patch:
        patch.setattr(cli.os, "getuid", lambda: original_uid + 1)
        with pytest.raises(ValueError, match="owner-private"):
            cli.read_private(source)


async def test_interrupted_zero_count_run_is_not_silently_replayed(storage, monkeypatch):
    run, plan = prepared(storage)
    cli.write_private(
        run / "baseline.json",
        {
            "plan_sha256": plan["plan_sha256"],
            "status": "running",
            "model_calls": 0,
        },
    )
    provider = FakeProvider()
    fake_live(monkeypatch, provider)
    with pytest.raises(ValueError, match="Partial inference"):
        await cli.run_phase(run, "baseline")
    assert provider.calls == []


async def test_request_intent_is_saved_before_provider_dispatch(storage, monkeypatch):
    run, _ = prepared(storage)

    class ChecksIntent(FakeProvider):
        async def complete(self, system, user, **kwargs):
            state = cli.read_private(run / "baseline.json")
            assert state["model_calls"] == len(self.calls) + 1
            return await super().complete(system, user, **kwargs)

    provider = ChecksIntent()
    fake_live(monkeypatch, provider)
    assert (await cli.run_phase(run, "baseline"))["status"] == "captured_ungraded"
