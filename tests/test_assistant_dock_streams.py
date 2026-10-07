"""The dock shows the answer as it is written.

The dashboard has no JavaScript test runner, so, as in
`test_dashboard_effects.py`, this reads the source. What it holds is the
wiring a refactor could undo without anything else failing: the dock calling
the plain route again would look the same on a fast model and put the long
wait back on the local one.

The behaviour itself is covered where it can be run: the route and its frames
in `test_chat_stream.py`, the provider in `test_ollama_streaming.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "apps/web/src"
ASSISTANT = (WEB / "components/assistant.tsx").read_text(encoding="utf-8")
READER = (WEB / "lib/chat-stream.ts").read_text(encoding="utf-8")


def test_the_dock_asks_the_streaming_route_and_no_other() -> None:
    assert re.findall(r'fetch\("(/api/chat[^"]*)"', ASSISTANT) == ["/api/chat/stream"]


def test_every_kind_of_frame_is_handled() -> None:
    """`delta`, `done`, and the model stopping part way. The API sends no others."""
    assert 'frame.type === "delta"' in ASSISTANT
    assert 'frame.type === "done"' in ASSISTANT
    assert "frame.message" in ASSISTANT, "an error frame's message must reach the dock"
    for kind in ("delta", "done", "error"):
        assert f'type: "{kind}"' in READER


def test_the_finished_reply_replaces_what_was_shown_while_it_was_written() -> None:
    """Citations are read from the finished text and arrive last. Appending the
    reply as a second turn would show the answer twice, once without them."""
    assert "writing ? [...held.slice(0, -1), turn] : [...held, turn]" in ASSISTANT


def test_a_stream_that_ends_without_a_reply_says_so() -> None:
    """A dropped connection must not leave half an answer looking like a whole one."""
    assert "if (!finished)" in ASSISTANT
    assert "cut off before it finished" in ASSISTANT


def test_an_error_before_the_stream_starts_is_still_read_as_an_error() -> None:
    """A model that is down answers with a status code and the API's own message."""
    assert "!response.ok || !response.body" in ASSISTANT
    assert "body?.error?.message" in ASSISTANT


def test_a_chunk_is_not_assumed_to_be_a_line() -> None:
    """The network splits where it likes. Only text up to a newline is parsed,
    and the rest is kept for the next chunk."""
    assert 'buffer.split("\\n")' in READER
    assert "lines.pop()" in READER
    assert "stream: true" in READER, "a character split across two chunks must survive"
