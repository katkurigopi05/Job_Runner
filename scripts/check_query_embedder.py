"""Check a question can be embedded on this machine — `make query-embedder-check`.

Embeds one fixed question three times through Ollama: once loading the model,
once loaded, and once asking it to unload, then prints how long each took and
the vector's width. Nothing about the owner is sent; the question is fixed.
"""

from __future__ import annotations

import asyncio

from packages.core.config import get_settings
from packages.matching.query_vectors import QueryEmbeddingError, embed_question

SAMPLE = "data engineering roles that use Kafka"


async def main() -> int:
    settings = get_settings()
    print(f"model {settings.query_embed_model} at {settings.ollama_base_url}")
    try:
        first = await embed_question(SAMPLE)
        warm = await embed_question(SAMPLE)
        last = await embed_question(SAMPLE, keep_alive="0")
    except QueryEmbeddingError as exc:
        print(f"not working: {exc}")
        return 1
    print(f"width {len(first.values)}, compared with stored vectors of {first.space}")
    print(f"first question {first.seconds:.2f} s (includes loading), then {warm.seconds:.2f} s")
    print(f"unloaded after the last one ({last.seconds:.2f} s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
