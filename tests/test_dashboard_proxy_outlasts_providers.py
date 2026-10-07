"""The dashboard's proxy must not give up on the assistant before a model does.

Found on 2026-10-06. The owner asked "any companies required skill of kafka
list them" with the local model and was told "Could not reach the API. Is
`make api` running?". It was running, and it answered that question in 29 s
when asked directly: about 22 s of that is Ollama loading a 4.1 GB model it
had unloaded after one idle minute (`OLLAMA_KEEP_ALIVE=1m` on that machine).

The browser reaches the API through Next's rewrite (`next.config.ts`), and
Next drops a proxied request that has sent nothing back for 30 s. `/chat`
sends nothing until the model has finished, so the first question after a
pause was cut off at the proxy, the API's request was cancelled with it, and
the dashboard reported the one thing that was not true.

The API has its own limits, one per provider, and its errors name the cause
("the local model is not answering", "OpenRouter's allowance is spent"). The
proxy has to outlast all of them, or the owner reads the proxy's failure in
place of the API's reason.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NEXT_CONFIG = (ROOT / "apps" / "web" / "next.config.ts").read_text()
PROVIDERS = (ROOT / "packages" / "llm" / "provider.py").read_text()

#: What Next uses when the setting is absent (`proxy-request.js`, 15.5.4).
NEXT_DEFAULT_MS = 30_000


def _proxy_timeout_ms() -> int:
    """What the config hands Next, or Next's default when it hands it nothing.

    The constant has to be the one in use: a number declared and never passed
    to `proxyTimeout` would read as a fix here and change nothing.
    """
    declared = re.search(r"const PROXY_TIMEOUT_MS = ([\d_]+);", NEXT_CONFIG)
    in_use = re.search(r"proxyTimeout:\s*PROXY_TIMEOUT_MS\b", NEXT_CONFIG)
    if declared is None or in_use is None:
        return NEXT_DEFAULT_MS
    return int(declared.group(1).replace("_", ""))


def _slowest_provider_s() -> float:
    """The longest any provider waits on one request, read from the source.

    Read rather than listed, so a provider given a longer limit later moves
    this test with it.
    """
    waits = re.findall(r"(?:timeout=|REQUEST_TIMEOUT_S\s*=\s*)(\d+(?:\.\d+)?)", PROVIDERS)
    assert waits, "no request timeouts found in provider.py; this test reads them"
    return max(float(wait) for wait in waits)


def test_the_proxy_waits_longer_than_any_provider() -> None:
    slowest = _slowest_provider_s()

    assert _proxy_timeout_ms() / 1000 > slowest, (
        f"the dashboard's proxy gives up after {_proxy_timeout_ms() / 1000:.0f} s and a "
        f"provider may take {slowest:.0f} s, so a slow answer reads as an unreachable API"
    )
