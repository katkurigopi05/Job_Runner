"""A second model re-orders the assistant's search results.

The assistant's chunks are embedded with bge-small because it is fast: 366
chunks a second on the owner's M4, so the whole corpus in six minutes. It is
also the weakest of the models measured on 2026-10-05 (56 postings, two
questions each, 1,000-posting pool; "top 5" is the posting a question was
written from):

    bge-small alone                       natural 64%   paraphrased 11%
    bge-large for everything              natural 71%   paraphrased 29%
    Qwen3-Embedding-0.6B for everything   natural 80%   paraphrased 34%
    bge-small, then 0.6B on the top 30    natural 77%   paraphrased 36%

Re-scoring only the top 30 gets almost everything a full switch gets, with
nothing to re-embed: 30 chunks at 19 a second is about 1.6 s a question,
against two hours to backfill 141,603 chunks (13.6M tokens). Re-ordering the
top 100 was no better than the top 30. Qwen3-Embedding-4B is better still on
paraphrased questions and is 3.3 chunks a second, so about 9 s a question.

Three rules:

- **It only re-orders.** It is handed what the search found and returns the
  same items in a new order. Keywords still decide what is relevant
  (CLAUDE.md §14); a model that is confident about a posting the search did
  not find has no way to add it.
- **Off unless a model is named.** `CHAT_RERANK_MODEL` is empty as shipped. A
  fresh checkout does not download 1.2 GB or hold 0.7 GB in the API process
  to answer a question.
- **Failing is not fatal.** No model, a load error, an encode error: the
  search's own order stands and the answer says nothing was re-ranked.

It runs on this machine like every other part of retrieval, so §2.8 is
unchanged. This is a bi-encoder used as a second stage, not a cross-encoder:
it embeds the question and each candidate's best chunk and compares them.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
from typing import Protocol

import structlog

from packages.core.config import get_settings

log = structlog.get_logger(__name__)


#: Qwen3-Embedding's documented query format. Passages take no instruction.
QUERY_INSTRUCTION = (
    "Instruct: Given a job seeker's question, retrieve job postings that answer it\nQuery: "
)

#: Fastest of 8, 32 and 64 on the owner's M4 (18.7, 17.0 and 15.5 chunks a
#: second), and the lightest on memory.
BATCH_SIZE = 8


class Reranker(Protocol):
    name: str

    def scores(self, question: str, passages: list[str]) -> list[float]:
        """One score per passage, higher meaning a better answer to `question`."""
        ...


class EmbeddingReranker:
    """Re-scores passages with a sentence-transformers embedding model.

    Loaded in full precision on purpose. On the owner's M4 half precision was
    no faster and peaked at 2.7-3.1 GB against 0.85 GB.
    """

    def __init__(self, model_name: str) -> None:
        from sentence_transformers import SentenceTransformer

        self.name = model_name
        self._model = SentenceTransformer(model_name, processor_kwargs={"padding_side": "left"})
        self._model.max_seq_length = 512

    def scores(self, question: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        query = self._model.encode(
            [question], prompt=QUERY_INSTRUCTION, normalize_embeddings=True, show_progress_bar=False
        )[0]
        docs = self._model.encode(
            passages, batch_size=BATCH_SIZE, normalize_embeddings=True, show_progress_bar=False
        )
        return [float(score) for score in docs @ query]


@lru_cache(maxsize=1)
def get_reranker() -> Reranker | None:
    """The configured re-ranker, loaded once per process, or None.

    None when no model is named, and when the named one cannot be loaded: a
    missing download or a missing extra must not stop the assistant searching.
    """
    name = get_settings().chat_rerank_model.strip()
    if not name:
        return None
    try:
        reranker = EmbeddingReranker(name)
    except Exception as exc:  # noqa: BLE001 - search works without a re-ranker
        log.warning("reranker_unavailable", model=name, error=type(exc).__name__)
        return None
    log.info("reranker_loaded", model=name)
    return reranker


def reorder[T](items: Sequence[T], scores: Sequence[float]) -> list[T]:
    """`items`, best score first. Equal scores keep the order they came in."""
    if len(scores) != len(items):
        raise ValueError(f"{len(scores)} scores for {len(items)} items")
    order = sorted(range(len(items)), key=lambda index: -scores[index])
    return [items[index] for index in order]


__all__ = ["EmbeddingReranker", "Reranker", "get_reranker", "reorder"]
