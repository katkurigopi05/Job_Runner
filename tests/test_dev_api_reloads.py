"""`make api` has to survive its own reload while the dashboard is open.

Found on 2026-10-06, mid-change. `make api` runs uvicorn with `--reload`, and
a reload asks the old worker to stop. Uvicorn then waits for open connections
to close, and the dashboard holds one that never does: the live status stream
(`GET /events/applications`, CLAUDE.md §17). So with any dashboard tab open,
the first saved file left the API accepting nothing: its event loop idle in
`kevent`, one connection on port 8000, `/health` timing out, and the reloader
waiting on a worker that was waiting on the browser. It looked like a hang in
whatever had just been edited, and was not.

A bounded wait ends it. Three seconds is enough for an ordinary request and
short enough that a save still feels like a reload.
"""

from __future__ import annotations

import re
from pathlib import Path

MAKEFILE = (Path(__file__).resolve().parents[1] / "Makefile").read_text()


def _recipe(target: str) -> str:
    found = re.search(rf"^{target}:.*\n((?:\t.*\n)+)", MAKEFILE, re.M)
    assert found is not None, f"no `{target}` target in the Makefile"
    return found.group(1)


def test_the_dev_api_does_not_wait_forever_for_the_status_stream() -> None:
    recipe = _recipe("api")

    assert "--reload" in recipe, "this is only a hazard of the reloading server"
    waited = re.search(r"--timeout-graceful-shutdown[ =](\d+)", recipe)
    assert waited is not None, (
        "a reload waits for the dashboard's stream to close, which it never does"
    )
    assert 1 <= int(waited.group(1)) <= 10
