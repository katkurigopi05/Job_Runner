"""Embeddings for posting/profile similarity.

Two implementations behind one Protocol:

- `LexicalEmbedder` (default) — hashed, IDF-weighted bag of words. No model
  download, deterministic, runs anywhere. It measures **vocabulary overlap,
  not meaning**: it will score "Python backend engineer" against "Python
  backend developer" well and against "server-side engineer" poorly. That is
  a real limitation, and it is named here rather than hidden behind the word
  "embedding".
- `SentenceTransformerEmbedder` — BAAI/bge-small-en-v1.5 per CLAUDE.md §3.
  Genuinely semantic, needs a ~130MB model on first use.

The default is lexical so a fresh install works offline with no surprise
download. Set `EMBEDDING_BACKEND=sentence-transformers` for the real thing.
Both produce 384-dim unit vectors, so the column type and cosine maths are
identical and you can switch without a migration — but you must re-embed,
because vectors from different backends are not comparable.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import Protocol

import structlog

log = structlog.get_logger(__name__)

#: Matches the Vector(384) column and bge-small's output width.
EMBEDDING_DIM = 384

_TOKEN_RE = re.compile(r"[a-z0-9+#.]+")

#: Words too common in job text to carry signal. Kept as text so the
#: formatter leaves it readable.
_STOPWORDS_TEXT = """
a ability an and applicant apply are as at be by candidate candidates
company excellent experience for from good great has have help in
including is it its job like new of on opportunity or other our own part
please position role strong team that the to using we will with work
working year years you your
"""

_STOPWORDS = frozenset(_STOPWORDS_TEXT.split())


class Embedder(Protocol):
    name: str
    dim: int

    def encode(self, texts: list[str]) -> list[list[float]]: ...


def tokenize(text: str) -> list[str]:
    return [
        token
        for token in _TOKEN_RE.findall(text.lower())
        if len(token) > 1 and token not in _STOPWORDS
    ]


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity, clamped to [0, 1] for non-negative vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return max(0.0, min(1.0, dot / (norm_a * norm_b)))


class LexicalEmbedder:
    """Hashed bag of words with sublinear term weighting.

    Deterministic across processes and machines — the hash is sha256, not
    Python's randomized `hash()`, so vectors written on one run still match on
    the next.
    """

    name = "lexical"

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self.dim = dim

    def _bucket(self, token: str) -> int:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        return int.from_bytes(digest[:4], "big") % self.dim

    def encode_one(self, text: str) -> list[float]:
        counts = Counter(tokenize(text))
        if not counts:
            return [0.0] * self.dim

        vector = [0.0] * self.dim
        for token, count in counts.items():
            # Sublinear scaling: the tenth "Python" says little the first did not.
            vector[self._bucket(token)] += 1.0 + math.log(count)

        norm = math.sqrt(sum(v * v for v in vector))
        if norm > 0:
            vector = [v / norm for v in vector]
        return vector

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [self.encode_one(text) for text in texts]


class SentenceTransformerEmbedder:
    """BAAI/bge-small-en-v1.5, per CLAUDE.md §3. Downloads on first use."""

    name = "sentence-transformers"

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5") -> None:
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name)
        self.dim = int(self._model.get_sentence_embedding_dimension())
        if self.dim != EMBEDDING_DIM:
            raise ValueError(
                f"{model_name} produces {self.dim}-dim vectors but the schema "
                f"column is {EMBEDDING_DIM}-dim"
            )

    def encode(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(texts, normalize_embeddings=True)
        return [list(map(float, v)) for v in vectors]


_embedder: Embedder | None = None


def get_embedder() -> Embedder:
    """The configured embedder, falling back loudly rather than silently."""
    global _embedder
    if _embedder is not None:
        return _embedder

    import os

    backend = os.environ.get("EMBEDDING_BACKEND", "lexical").lower()
    if backend == "sentence-transformers":
        try:
            _embedder = SentenceTransformerEmbedder()
        except Exception as exc:  # noqa: BLE001 - missing package or model
            log.warning(
                "sentence_transformers_unavailable_using_lexical",
                error=type(exc).__name__,
            )
            _embedder = LexicalEmbedder()
    else:
        _embedder = LexicalEmbedder()

    log.info("embedder_selected", backend=_embedder.name)
    return _embedder


def set_embedder(embedder: Embedder | None) -> None:
    global _embedder
    _embedder = embedder
