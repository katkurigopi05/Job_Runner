"""A question's Qwen3-Embedding-4B vector, made on this machine through Ollama.

The chunk vectors in `posting_chunk_vectors` were made on a Colab TPU from
public posting text. A question is the owner's own words, and the assistant's
search runs on this machine (CLAUDE.md §14), so its vector is made here: the
4-bit build of the same model, through the Ollama daemon the local chat model
already uses. Measured 2026-11-02 on the 112 benchmark questions against the
TPU's chunk vectors, it found the right posting in the top five as often as the
TPU's own question vectors did (34 and 11 of 56, against 35 and 10), at 0.1 to
0.2 s a question once loaded and about 3 s to load.

Three things this holds:

- **The question never leaves the machine.** Ollama at an address other than
  this machine's, or a model Ollama runs on its own servers (`:cloud`), is
  refused before anything is sent.
- **The question is worded as the vectors were measured**: Qwen's retrieval
  instruction in front (`QUERY_PREFIX`), the chunks without one.
- **A vector that cannot be compared is an error, not a guess**: another
  width, all zeros, or not finite. What the search does without one is the
  caller's decision, and it should say so.

Nothing calls this yet; the search change that does is measured first.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import httpx
import structlog

from packages.core.config import get_settings
from packages.core.models_chunks import LARGE_EMBEDDING_DIM
from packages.llm.audit import is_cloud_model, is_loopback
from packages.matching.chunk_vectors import QWEN_4B

log = structlog.get_logger(__name__)

#: The instruction the 2026-11-02 measurement used. Changing it changes what
#: the vectors mean; measure again first.
QUERY_PREFIX = (
    "Instruct: Given a job seeker's question, retrieve job postings that answer it\nQuery: "
)
#: Seconds to wait. The first question after the model unloads also loads it
#: (3.3 s measured); a cold disk takes longer.
TIMEOUT_S = 60.0


class QueryEmbeddingError(RuntimeError):
    """Why no vector was made, in words that say what to do about it."""


@dataclass(frozen=True)
class QueryVector:
    #: The stored vectors this one compares with (`posting_chunk_vectors.embedding_model`).
    space: str
    #: The Ollama model that made it.
    build: str
    values: tuple[float, ...]
    seconds: float


def _refuse_remote(base_url: str, model: str) -> None:
    if not is_loopback(base_url):
        raise QueryEmbeddingError(
            f"OLLAMA_BASE_URL points at {base_url}, not this machine, so the question would "
            "leave it. The question is embedded locally only (CLAUDE.md §14)."
        )
    if is_cloud_model(model):
        raise QueryEmbeddingError(
            f"{model} runs on Ollama's servers, so the question would leave this machine. "
            "Set QUERY_EMBED_MODEL to a local build."
        )


def _reason(response: httpx.Response) -> str:
    try:
        return str(response.json().get("error", ""))
    except ValueError:
        return response.text[:200]


def _checked(raw: object) -> tuple[float, ...]:
    """The first embedding in Ollama's answer, normalised, or why it cannot be used."""
    vectors = raw.get("embeddings") if isinstance(raw, dict) else None
    if not isinstance(vectors, list) or not vectors or not isinstance(vectors[0], list):
        raise QueryEmbeddingError("Ollama answered without an embedding")
    vector = vectors[0]
    if len(vector) != LARGE_EMBEDDING_DIM:
        raise QueryEmbeddingError(
            f"the vector is {len(vector)} wide, not {LARGE_EMBEDDING_DIM}: "
            "QUERY_EMBED_MODEL is not Qwen3-Embedding-4B"
        )
    values = [float(v) for v in vector]
    if not all(math.isfinite(v) for v in values):
        raise QueryEmbeddingError("the vector has values that are not finite")
    norm = math.sqrt(sum(v * v for v in values))
    if norm == 0.0:
        raise QueryEmbeddingError("the vector is all zeros")
    return tuple(v / norm for v in values)


async def _post(
    client: httpx.AsyncClient | None, url: str, body: dict[str, object]
) -> httpx.Response:
    if client is not None:
        return await client.post(url, json=body, timeout=TIMEOUT_S)
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as own:
        return await own.post(url, json=body)


async def embed_question(
    question: str,
    *,
    keep_alive: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> QueryVector:
    """The question's vector, comparable with the Qwen chunk vectors.

    `keep_alive` overrides QUERY_EMBED_KEEP_ALIVE for this call; "0" unloads
    the model as soon as it has answered.
    """
    text = question.strip()
    if not text:
        raise ValueError("an empty question has no vector")
    settings = get_settings()
    base_url, model = settings.ollama_base_url, settings.query_embed_model
    _refuse_remote(base_url, model)
    body: dict[str, object] = {
        "model": model,
        "input": QUERY_PREFIX + text,
        "options": {"num_ctx": settings.query_embed_num_ctx},
        "keep_alive": settings.query_embed_keep_alive if keep_alive is None else keep_alive,
    }
    started = time.perf_counter()
    try:
        response = await _post(client, base_url.rstrip("/") + "/api/embed", body)
    except httpx.TransportError as exc:
        raise QueryEmbeddingError(
            f"Ollama is not reachable at {base_url}. Start it with `ollama serve`."
        ) from exc
    if response.status_code >= 400:
        reason = _reason(response)
        if response.status_code == 404 or "not found" in reason.lower():
            raise QueryEmbeddingError(f"{model} is not pulled. Run `ollama pull {model}`.")
        raise QueryEmbeddingError(f"Ollama refused the question ({response.status_code}): {reason}")
    try:
        raw = response.json()
    except ValueError as exc:
        raise QueryEmbeddingError("Ollama answered with something that is not JSON") from exc
    values = _checked(raw)
    seconds = time.perf_counter() - started
    log.info("query_vector", build=model, seconds=round(seconds, 3))
    return QueryVector(space=QWEN_4B, build=model, values=values, seconds=seconds)


__all__ = ["QUERY_PREFIX", "QueryEmbeddingError", "QueryVector", "embed_question"]
