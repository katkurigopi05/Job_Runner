"""A question's Qwen vector is made on this machine, the way the stored ones were measured.

The chunk vectors were made on a Colab TPU (2026-11-02). Asked on the Mac with
the 4-bit build through Ollama, the same 112 questions found the right posting
in the top five as often as the TPU's own question vectors (34 and 11 of 56,
against 35 and 10), at 0.1-0.2 s a question. The question is the owner's own
words, so it is embedded here and nowhere else (CLAUDE.md §14).
"""

from __future__ import annotations

import json
import math

import httpx
import pytest

from packages.core.config import get_settings
from packages.matching import query_vectors as QV
from packages.matching.chunk_vectors import QWEN_4B

WIDTH = 2560


@pytest.fixture(autouse=True)
def local_ollama(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.delenv("QUERY_EMBED_MODEL", raising=False)
    monkeypatch.delenv("QUERY_EMBED_KEEP_ALIVE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _answer(vector: list[float]):
    def handler(request: httpx.Request) -> httpx.Response:
        handler.seen.append(json.loads(request.content))
        handler.urls.append(str(request.url))
        # Written by hand: a NaN is not strict JSON, and a daemon could send one.
        body = json.dumps({"model": "x", "embeddings": [vector]}, allow_nan=True)
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    handler.seen, handler.urls = [], []
    return handler


async def test_the_question_is_sent_as_the_stored_vectors_were_measured():
    handler = _answer([2.0] * WIDTH)

    result = await QV.embed_question("roles using Kafka", client=_client(handler))

    body = handler.seen[0]
    assert handler.urls == ["http://localhost:11434/api/embed"]
    assert body["model"] == "hf.co/Qwen/Qwen3-Embedding-4B-GGUF:Q4_K_M"
    assert body["input"] == QV.QUERY_PREFIX + "roles using Kafka"
    assert body["options"] == {"num_ctx": 1024}
    assert body["keep_alive"] == "5m"
    assert result.space == QWEN_4B
    assert len(result.values) == WIDTH
    assert math.isclose(sum(v * v for v in result.values), 1.0, rel_tol=1e-6)


async def test_a_call_can_have_the_model_unloaded_at_once():
    handler = _answer([1.0] * WIDTH)

    await QV.embed_question("roles using Kafka", keep_alive="0", client=_client(handler))

    assert handler.seen[0]["keep_alive"] == "0"


async def test_ollama_not_running_says_how_to_start_it():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(QV.QueryEmbeddingError, match="ollama serve"):
        await QV.embed_question("roles using Kafka", client=_client(handler))


async def test_a_model_not_pulled_names_the_command_that_pulls_it():
    def handler(request):
        return httpx.Response(404, json={"error": 'model "x" not found, try pulling it first'})

    with pytest.raises(QV.QueryEmbeddingError) as raised:
        await QV.embed_question("roles using Kafka", client=_client(handler))

    assert "ollama pull hf.co/Qwen/Qwen3-Embedding-4B-GGUF:Q4_K_M" in str(raised.value)


@pytest.mark.parametrize(
    "vector",
    [[1.0] * 384, [0.0] * WIDTH, [1.0] * (WIDTH - 1) + [float("nan")]],
    ids=["another width", "all zeros", "not finite"],
)
async def test_a_vector_that_cannot_be_compared_is_refused(vector):
    with pytest.raises(QV.QueryEmbeddingError):
        await QV.embed_question("roles using Kafka", client=_client(_answer(vector)))


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("OLLAMA_BASE_URL", "https://ollama.com"),
        ("OLLAMA_BASE_URL", "http://192.168.1.20:11434"),
        ("QUERY_EMBED_MODEL", "qwen3-embedding:4b-cloud"),
    ],
)
async def test_the_question_never_leaves_this_machine(monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    get_settings.cache_clear()
    handler = _answer([1.0] * WIDTH)

    with pytest.raises(QV.QueryEmbeddingError, match="leave"):
        await QV.embed_question("roles using Kafka", client=_client(handler))

    assert handler.seen == [], "nothing was sent"


async def test_an_empty_question_is_refused_before_anything_is_sent():
    handler = _answer([1.0] * WIDTH)

    with pytest.raises(ValueError):
        await QV.embed_question("   ", client=_client(handler))

    assert handler.seen == []
