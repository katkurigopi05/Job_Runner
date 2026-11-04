"""Auto resolves a supported loaded model without loading or switching weights."""

from __future__ import annotations

import ast
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from pydantic import SecretStr

from packages.llm import kaggle, privacy
from packages.llm.audit import read_trail
from packages.llm.provider import LLMError


def health(model=kaggle.QWEN38_MODEL, **changes):
    return {
        "state": "ready",
        "protocol": 1,
        "context_tokens": 4096,
        "model": model,
        "helper": kaggle.HELPERS[model],
        "gpu_count": 2,
        "thinking": False,
        "model_sha256": kaggle.QWEN38_SHA256,
        **changes,
    }


@pytest.fixture
def provider():
    return kaggle.KaggleProvider(
        connection=kaggle.Connection(
            server_url=SecretStr(
                "https://kkb-production.jupyter-proxy.kaggle.net/k/123/test-secret/proxy"
            ),
            kernel_id=UUID(int=1),
        )
    )


def test_health_discovers_both_helpers_without_inference_or_loading():
    namespace = {}
    for model, name in kaggle.HELPERS.items():

        def complete(*args):
            pytest.fail("Health must not generate")

        complete.jobrunner_health = lambda m=model: health(m)
        namespace[name] = complete
    result = json.loads(eval(kaggle.HEALTH_EXPRESSION, namespace))
    assert {row["model"] for row in result["helpers"]} == set(kaggle.HELPERS)
    assert json.loads(eval(kaggle.HEALTH_EXPRESSION, {})) == {"helpers": []}


@pytest.mark.parametrize("models", [[kaggle.MODEL], [kaggle.QWEN38_MODEL], list(kaggle.HELPERS)])
async def test_auto_chooses_ready_model_preferring_27b(provider, monkeypatch, models):
    monkeypatch.setattr(
        provider, "_exchange", AsyncMock(return_value={"helpers": [health(m) for m in models]})
    )
    status = await provider.health()
    expected = kaggle.QWEN38_MODEL if kaggle.QWEN38_MODEL in models else kaggle.MODEL
    assert status.state == "ready" and status.model == provider.model == expected
    assert kaggle.MODEL_LABELS[expected] in status.detail


@pytest.mark.parametrize(
    "changes",
    [
        {"model_sha256": "wrong"},
        {"gpu_count": 1},
        {"gpu_count": True},
        {"thinking": True},
        {"protocol": True},
        {"context_tokens": 8192},
        {"helper": "unexpected_helper"},
        {"state": []},
    ],
)
async def test_invalid_27b_readiness_never_receives_private_prompt(provider, monkeypatch, changes):
    exchange = AsyncMock(return_value={"helpers": [health(**changes)]})
    monkeypatch.setattr(provider, "_exchange", exchange)
    with pytest.raises(LLMError):
        await provider.complete("PRIVATE SYSTEM", "PRIVATE USER")
    assert exchange.await_count == 1
    assert "PRIVATE" not in exchange.call_args.args[0]


async def test_auto_can_use_8b_when_27b_is_unready_at_start(provider, monkeypatch):
    monkeypatch.setattr(
        provider,
        "_exchange",
        AsyncMock(
            return_value={
                "helpers": [health(state="model_not_ready"), health(kaggle.MODEL)],
            }
        ),
    )
    assert (await provider.health()).model == kaggle.MODEL


async def test_resolved_model_never_switches_when_kernel_changes(provider, monkeypatch):
    exchange = AsyncMock(side_effect=[{"helpers": [health()]}, {"helpers": [health(kaggle.MODEL)]}])
    monkeypatch.setattr(provider, "_exchange", exchange)
    assert (await provider.health()).model == kaggle.QWEN38_MODEL
    with pytest.raises(LLMError):
        await provider.complete("PRIVATE SYSTEM", "PRIVATE USER")
    assert exchange.await_count == 2
    assert all("PRIVATE" not in call.args[0] for call in exchange.call_args_list)


async def test_selected_helper_model_is_used_in_payload_and_audit(provider, monkeypatch):
    exchange = AsyncMock(
        side_effect=[
            {"helpers": [health()]},
            {
                "model": kaggle.QWEN38_MODEL,
                "status": "ok",
                "text": "answer",
            },
        ]
    )
    monkeypatch.setattr(provider, "_exchange", exchange)
    assert await provider.complete("system", "user") == "answer"
    node = ast.parse(exchange.call_args.args[0], mode="eval").body
    assert node.func.id == "jobrunner_qwen38_complete"
    assert json.loads(ast.literal_eval(node.args[0]))["model"] == kaggle.QWEN38_MODEL
    assert read_trail()[-1].model == kaggle.QWEN38_MODEL


@pytest.mark.parametrize("endpoint", ["/chat", "/chat/stream"])
@pytest.mark.parametrize("selection", [False, True])
async def test_plain_stream_and_selection_label_actual_masked_model(
    client, monkeypatch, provider, endpoint, selection
):
    from apps.api.chat_selection import EvidenceItem
    from apps.api.routers import chat, chat_stream
    from packages.matching.retrieve import Passage, Retrieval

    async def exchange(expression, **kwargs):
        if "jobrunner_health" in expression:
            return {"helpers": [health()]}
        assert "Alex Morgan" not in expression
        assert "JR_PRIVATE_" in expression
        return {
            "model": kaggle.QWEN38_MODEL,
            "status": "ok",
            "text": '{"ids":["P1"]}' if selection else "Supported response.",
        }

    monkeypatch.setattr(provider, "_exchange", exchange)
    monkeypatch.setattr(
        privacy,
        "load_identifiers",
        AsyncMock(return_value=privacy.Identifiers(names=("Alex Morgan",))),
    )
    monkeypatch.setattr(
        chat.llm_router, "build_provider", lambda _: privacy.protect_provider(provider)
    )
    ready = chat.Ready(
        selected="kaggle",
        include_mail=False,
        prompt="Alex Morgan asks a question",
        found=Retrieval(
            attempted=selection,
            searched=1 if selection else 0,
            passages=(
                Passage(
                    label="P1",
                    posting_id=UUID(int=2),
                    title="Data Engineer",
                    company="Example",
                    location="Remote - USA",
                    url="https://example.test/job",
                    excerpt="Uses Kafka",
                    application_status=None,
                ),
            )
            if selection
            else (),
        ),
        question="Which role uses Kafka?",
        selection_evidence=(EvidenceItem(id="P1", text="Uses Kafka", source_label="P1"),)
        if selection
        else None,
    )
    monkeypatch.setattr(chat, "prepare", AsyncMock(return_value=ready))
    monkeypatch.setattr(chat_stream, "_prepared", AsyncMock(return_value=ready))
    response = await client.post(endpoint, json={"message": "hello", "provider": "kaggle"})
    assert response.status_code == 200
    reply = (
        response.json()
        if endpoint == "/chat"
        else json.loads(response.text.splitlines()[-1])["reply"]
    )
    assert reply["model"] == kaggle.QWEN38_MODEL
    assert (
        reply["provider"] == "kaggle" and reply["local"] is False and reply["shared_mail"] is False
    )


def test_27b_helper_refuses_truncation_and_checks_context_before_generation():
    from scripts.kaggle_qwen38_helper import install

    engine = SimpleNamespace(
        n_ctx=lambda: 4096,
        metadata={"general.name": "Qwen3.8-27B"},
        tokenize=lambda *args, **kwargs: [1] * 4000,
        reset=lambda: None,
        create_completion=lambda **kwargs: pytest.fail("too long"),
    )
    tokenizer = SimpleNamespace(apply_chat_template=lambda *args, **kwargs: "prompt")
    identity = health()
    helper = install(engine, tokenizer, identity)
    payload = {
        "model": kaggle.QWEN38_MODEL,
        "system": "system",
        "user": "user",
        "temperature": 0,
        "max_tokens": 600,
    }
    assert json.loads(helper(json.dumps(payload)))["status"] == "too_long"
    engine.tokenize = lambda *args, **kwargs: [1]
    engine.create_completion = lambda **kwargs: {
        "choices": [{"finish_reason": "length", "text": "fragment"}]
    }
    assert json.loads(helper(json.dumps(payload)))["status"] == "truncated"
    assert helper.jobrunner_health()["model"] == kaggle.QWEN38_MODEL
    with pytest.raises(ValueError):
        install(engine, tokenizer, {**identity, "model_sha256": "wrong"})


def test_setup_probe_recognizes_loaded_qwen_without_running_it():
    from packages.llm import kaggle_setup

    def complete(*args):
        pytest.fail("Setup readiness must never generate")

    complete.jobrunner_health = lambda: health()
    facts = json.loads(
        eval(
            kaggle_setup.PROBE,
            {
                "jobrunner_qwen38_complete": complete,
                "jobrunner27_engine": object(),
                "jobrunner27_tokenizer": object(),
                "jobrunner27_identity": health(),
            },
        )
    )
    assert facts == {"model": False, "helper": True, "qwen38_model": True}
