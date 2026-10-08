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

## The other kind: a model that judges

Those three rules were measured against the real search the same week and the
gain did not survive it (CLAUDE.md §21). On 2026-10-08 a cross-encoder was
tried on the same questions, 49 postings still open: a model that reads the
question and one passage together and scores whether the passage answers it.
Right posting in the five shown, natural questions, against 37 as shipped:

    the search's own list, re-ordered                      39
    bge-small's vector top 50 alone, re-ordered            34
    the list and the top 50 together, re-ordered           43
    the same, its order fused with the search's            45

with the passage read under a line naming the title, company and place. With
the chunk alone every row was worse than the search (24 to 27).
`cross-encoder/ms-marco-MiniLM-L-12-v2` (33M parameters) and
`BAAI/bge-reranker-base` (278M) gave the same counts. Questions that
paraphrase a posting did not move, 2 to 4 of 49, with either.

So a model of this kind is used differently, and `judges` on the re-ranker
says which kind it is:

- **It may be handed postings the keywords did not find** (`retrieve.py`),
  and keeps one only when it scores it above zero. The score is the model's
  own number before squashing, and zero is its line between "answers the
  question" and "does not". A likeness has no such line, which is why the
  first kind is never handed any.
- **It reads the title, company and place over the chunk.**
- **Its order is fused with the search's** (`fuse`), not put in its place.
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

#: What the cross-encoders were measured at: 246 pairs a second for the 33M
#: model on the owner's M4.
CROSS_BATCH_SIZE = 16

#: The constant of Reciprocal Rank Fusion, as `retrieve.RRF_K` and
#: `hybrid.py` have it: borrowed, not fitted to the questions it is measured on.
RRF_K = 60


class Reranker(Protocol):
    name: str
    #: True when a score above zero is the model saying the passage answers
    #: the question (a cross-encoder). False when it is a likeness, which
    #: orders passages and says nothing about any one of them.
    judges: bool

    def scores(self, question: str, passages: list[str]) -> list[float]:
        """One score per passage, higher meaning a better answer to `question`."""
        ...


class EmbeddingReranker:
    """Re-scores passages with a sentence-transformers embedding model.

    Loaded in full precision on purpose. On the owner's M4 half precision was
    no faster and peaked at 2.7-3.1 GB against 0.85 GB.
    """

    judges = False

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


class CrossEncoderReranker:
    """Scores a passage by reading it together with the question.

    The score is the model's logit, not its sigmoid: the order is the same
    and zero stays the model's own line, where a squashed score would need a
    second number (0.5) kept in step with it.
    """

    judges = True

    def __init__(self, model_name: str) -> None:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self.name = model_name
        self._torch = torch
        # Where sentence-transformers puts bge-small, so the two share what
        # the GPU costs to use at all: about 1 GB on the owner's M4 before
        # any weights, against 0.13 GB for this model's.
        self._device = (
            "mps"
            if torch.backends.mps.is_available()
            else "cuda"
            if torch.cuda.is_available()
            else "cpu"
        )
        self._tokenizer = AutoTokenizer.from_pretrained(model_name)
        self._model = (
            AutoModelForSequenceClassification.from_pretrained(model_name).to(self._device).eval()
        )

    def scores(self, question: str, passages: list[str]) -> list[float]:
        torch = self._torch
        scores = [0.0] * len(passages)
        # Shortest first, so a batch is padded to nearly one width.
        order = sorted(range(len(passages)), key=lambda index: len(passages[index]))
        for at in range(0, len(order), CROSS_BATCH_SIZE):
            batch = order[at : at + CROSS_BATCH_SIZE]
            encoded = self._tokenizer(
                [question] * len(batch),
                [passages[index] for index in batch],
                padding=True,
                truncation=True,
                max_length=512,
                return_tensors="pt",
            ).to(self._device)
            with torch.inference_mode():
                logits = self._model(**encoded).logits
            for index, score in zip(
                batch, logits.reshape(len(batch)).float().cpu().tolist(), strict=True
            ):
                scores[index] = score
        return scores


def _is_cross_encoder(model_name: str) -> bool:
    """Whether the named model scores a question and a passage together.

    Read from the model's own description of itself, so `CHAT_RERANK_MODEL`
    stays the one setting. A second one saying which kind the first names
    could disagree with it. One score only: a classifier with several labels
    has no zero that means "this answers the question".

    A model whose description cannot be read is taken for an embedding model,
    which is what the setting named before there were two kinds. If it is not
    one, loading it fails and `get_reranker` says so.
    """
    try:
        from transformers import AutoConfig

        config = AutoConfig.from_pretrained(model_name)
    except Exception as exc:  # noqa: BLE001 - decided again, and reported, at load
        log.warning("reranker_kind_unread", model=model_name, error=type(exc).__name__)
        return False
    architectures = getattr(config, "architectures", None) or []
    return getattr(config, "num_labels", 0) == 1 and any(
        name.endswith("ForSequenceClassification") for name in architectures
    )


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
        reranker: Reranker = (
            CrossEncoderReranker(name) if _is_cross_encoder(name) else EmbeddingReranker(name)
        )
    except Exception as exc:  # noqa: BLE001 - search works without a re-ranker
        log.warning("reranker_unavailable", model=name, error=type(exc).__name__)
        return None
    log.info("reranker_loaded", model=name, judges=reranker.judges)
    return reranker


def reorder[T](items: Sequence[T], scores: Sequence[float]) -> list[T]:
    """`items`, best score first. Equal scores keep the order they came in."""
    if len(scores) != len(items):
        raise ValueError(f"{len(scores)} scores for {len(items)} items")
    order = sorted(range(len(items)), key=lambda index: -scores[index])
    return [items[index] for index in order]


def fuse[T](items: Sequence[T], scores: Sequence[float], *, ranked: int) -> list[T]:
    """`items` ordered by the search's rank and the model's rank together.

    The first `ranked` items are in the search's order. Any after them were
    not found by the search: each takes the rank after its last, so none is
    ranked above another by a search that ranked neither. Reciprocal Rank
    Fusion of that with the order `scores` puts them in.

    Ranks and not scores, for the reason `hybrid.py` gives: the two share no
    scale. A tie goes to the model's order, and equal scores keep the order
    the items came in, so with nothing to say the model changes nothing.
    """
    if len(scores) != len(items):
        raise ValueError(f"{len(scores)} scores for {len(items)} items")
    by_model = sorted(range(len(items)), key=lambda index: -scores[index])
    value = {
        index: 1.0 / (RRF_K + model_rank) + 1.0 / (RRF_K + min(index + 1, ranked + 1))
        for model_rank, index in enumerate(by_model, start=1)
    }
    return [items[index] for index in sorted(by_model, key=lambda index: -value[index])]


__all__ = [
    "CrossEncoderReranker",
    "EmbeddingReranker",
    "Reranker",
    "fuse",
    "get_reranker",
    "reorder",
]
