"""Check the saved Kaggle connection end to end: `make kaggle-check`.

Sends one short prompt through the same path the assistant uses (contact
masking and the audit trail included) and says whether Qwen3 8B answered. On
failure it prints the provider's reason, which names the problem and the fix.
Never prints the session link or the kernel id: both are credentials.
"""

from __future__ import annotations

import asyncio
import time

from packages.core.config import get_settings
from packages.llm.kaggle import saved_when
from packages.llm.provider import LLMError, build_provider

SYSTEM = "You answer in one word."
PROBE = "Reply with the word ready."


async def check() -> int:
    path = get_settings().kaggle_connection_file
    try:
        provider = build_provider("kaggle")
    except LLMError as exc:
        print(f"Not connected. {exc}")
        return 1
    print(f"Testing the Kaggle connection saved {saved_when(path)}...")
    started = time.perf_counter()
    try:
        await provider.complete(SYSTEM, PROBE, max_tokens=8, temperature=0.0)
    except LLMError as exc:
        print(f"Not working after {time.perf_counter() - started:.0f}s. {exc}")
        return 1
    print(
        f"Connected: Qwen3 8B answered in {time.perf_counter() - started:.1f}s. "
        "Choose Kaggle GPU (remote) in the assistant."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(check()))
