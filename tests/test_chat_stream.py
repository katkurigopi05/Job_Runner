"""`POST /chat/stream`: the assistant's answer, shown as it is written.

`POST /chat` sends nothing until the model has finished. On the owner's
machine that is about 5 s of reading the prompt and then the answer at about
12 tokens a second (CLAUDE.md §14), so a 150-token answer was 17 s of an empty
dock. The stream sends each piece as the model writes it and closes with the
same reply `/chat` returns.

What the stream may not change:

- **The rules that run before the model.** The §2.2 refusal, the crawl command
  and the provider checks are the same code, and answer without a model.
- **Citations.** They are read out of the finished text, so they arrive in the
  closing frame, with everything else the dock lists under an answer.
- **No fallback.** A model that is down is the same error `/chat` gives.

A real server on a real socket, as in `test_status_stream.py`: httpx's
in-process transport builds a response only after the app returns, so it
cannot show that a piece arrived before the answer was finished.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient

import apps.api.routers.chat as chat_module
import apps.api.routers.chat_stream as stream_module
from packages.llm.provider import LLMError
from packages.matching.retrieve import Passage, Retrieval

#: How long a test waits for one frame. Generous: a frame that never comes is
#: a failure, and a slow machine is not.
FRAME_TIMEOUT_S = 10.0

QUESTION = {"message": "how many are waiting on me?"}
PIECES = ["Two applications", " are waiting", " on you."]
ANSWER = "".join(PIECES)


@pytest_asyncio.fixture
async def live_api(committing_sessionmaker, monkeypatch) -> AsyncIterator[AsyncClient]:
    """uvicorn on a spare port, in the test's own event loop."""
    import uvicorn

    import packages.core.db as core_db
    from apps.api.main import app

    monkeypatch.setattr(core_db, "get_sessionmaker", lambda: committing_sessionmaker)

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve())
    try:
        for _ in range(500):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started, "the test server did not come up"
        port = server.servers[0].sockets[0].getsockname()[1]
        async with AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=FRAME_TIMEOUT_S) as ac:
            yield ac
    finally:
        server.should_exit = True
        await serving


class _Writer:
    """A local model that writes in pieces, and can be made to wait or to fail."""

    name = "ollama"
    model = "some-local-model"

    def __init__(
        self,
        pieces: list[str],
        *,
        hold_last: asyncio.Event | None = None,
        fail_at: int | None = None,
    ) -> None:
        self.pieces = pieces
        self.hold_last = hold_last
        self.fail_at = fail_at
        self.stopped = asyncio.Event()
        self.asked: list[tuple[str, str, int, float]] = []

    async def stream(self, system: str, user: str, *, max_tokens: int, temperature: float):  # noqa: ANN201
        self.asked.append((system, user, max_tokens, temperature))
        try:
            for index, piece in enumerate(self.pieces):
                if index == self.fail_at:
                    raise LLMError("Ollama call failed: model runner has stopped")
                if self.hold_last is not None and index == len(self.pieces) - 1:
                    await self.hold_last.wait()
                yield piece
        finally:
            self.stopped.set()

    async def complete(self, system: str, user: str, *, max_tokens: int, temperature: float) -> str:
        return "".join(self.pieces)


class _Whole:
    """A provider with no `stream`, like the remote ones."""

    name = "gemini"
    model = "gemini-flash"

    async def complete(self, system: str, user: str, *, max_tokens: int, temperature: float) -> str:
        return ANSWER


def _answered_by(monkeypatch, provider: Any) -> list[str]:
    asked: list[str] = []

    def build(name: str | None = None) -> Any:
        asked.append(name or "default")
        return provider

    monkeypatch.setattr(chat_module.llm_router, "build_provider", build)
    return asked


class _Frames:
    """Reads one frame at a time off an open response, with a limit on each."""

    def __init__(self, response: Any) -> None:
        self._lines = response.aiter_lines()

    async def next(self) -> dict[str, Any]:
        while True:
            line = await asyncio.wait_for(anext(self._lines), FRAME_TIMEOUT_S)
            if line.strip():
                return json.loads(line)

    async def rest(self) -> list[dict[str, Any]]:
        frames = []
        while True:
            try:
                frames.append(await self.next())
            except StopAsyncIteration:
                return frames


async def _all_frames(client: AsyncClient, body: dict[str, Any]) -> list[dict[str, Any]]:
    async with client.stream("POST", "/chat/stream", json=body) as response:
        assert response.status_code == 200, await response.aread()
        return await _Frames(response).rest()


async def test_the_first_words_arrive_before_the_answer_is_finished(
    live_api: AsyncClient, monkeypatch
) -> None:
    """The point of the route. The last piece is held back until two have been read."""
    release = asyncio.Event()
    _answered_by(monkeypatch, _Writer(PIECES, hold_last=release))

    async with live_api.stream("POST", "/chat/stream", json=QUESTION) as response:
        frames = _Frames(response)
        first, second = await frames.next(), await frames.next()
        assert [first, second] == [
            {"type": "delta", "text": "Two applications"},
            {"type": "delta", "text": " are waiting"},
        ]
        assert not release.is_set(), "both arrived while the model was still writing"

        release.set()
        last, done = await frames.rest()

    assert last == {"type": "delta", "text": " on you."}
    assert done["type"] == "done"
    assert done["reply"]["reply"] == ANSWER


async def test_the_closing_frame_is_the_reply_the_plain_route_gives(
    live_api: AsyncClient, monkeypatch
) -> None:
    """One reply, two ways of delivering it. Compared whole, so a field added to one
    and not the other fails here."""
    _answered_by(monkeypatch, _Writer(PIECES))

    plain = (await live_api.post("/chat", json=QUESTION)).json()
    *_, done = await _all_frames(live_api, QUESTION)

    assert done == {"type": "done", "reply": plain}
    assert plain["provider"] == "ollama" and plain["local"] is True


async def test_the_model_is_asked_the_way_the_plain_route_asks_it(
    live_api: AsyncClient, monkeypatch
) -> None:
    writer = _Writer(PIECES)
    _answered_by(monkeypatch, writer)

    await _all_frames(live_api, QUESTION)

    [(system, user, max_tokens, temperature)] = writer.asked
    assert system == chat_module.SYSTEM
    assert user.startswith("CONTEXT:\n") and user.endswith("QUESTION:\nhow many are waiting on me?")
    assert (max_tokens, temperature) == (600, 0.2)


async def test_citations_are_read_from_the_finished_text(
    live_api: AsyncClient, monkeypatch
) -> None:
    """A label can be split across two pieces. Only the whole answer can be read for them."""

    def posting(label: str, title: str) -> Passage:
        return Passage(
            label=label,
            posting_id=uuid.uuid4(),
            title=title,
            company="Northwind",
            location="San Francisco, CA",
            url=f"https://example.test/{label}",
            excerpt="Kafka in production.",
            application_status=None,
        )

    async def found(session: Any, question: str) -> Retrieval:
        return Retrieval(
            attempted=True,
            passages=(posting("P1", "Data Engineer"), posting("P2", "Platform Engineer")),
            searched=2,
        )

    monkeypatch.setattr(chat_module, "retrieve", found)
    _answered_by(monkeypatch, _Writer(["Data Engineer uses Kafka [P", "1]."]))

    *_, done = await _all_frames(live_api, {"message": "Which open roles use Kafka?"})

    cited = {source["label"]: source["cited"] for source in done["reply"]["sources"]}
    assert cited == {"P1": True, "P2": False}


async def test_the_response_refuses_to_be_compressed(live_api: AsyncClient, monkeypatch) -> None:
    """§17. Next's proxy re-encodes a proxied body, and a compressor buffers: the
    dock would connect and then show nothing until the answer was complete."""
    _answered_by(monkeypatch, _Writer(PIECES))

    async with live_api.stream(
        "POST", "/chat/stream", json=QUESTION, headers={"accept-encoding": "gzip, br"}
    ) as response:
        await response.aread()

    assert "no-transform" in response.headers["cache-control"]
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert "content-encoding" not in response.headers


async def test_a_protected_question_is_one_frame_and_no_model(
    live_api: AsyncClient, monkeypatch
) -> None:
    """§2.2, refused in code before any provider is built."""
    asked = _answered_by(monkeypatch, _Writer(PIECES))

    frames = await _all_frames(live_api, {"message": "what should I put for work authorization?"})

    [done] = frames
    assert done["type"] == "done"
    assert done["reply"]["provider"] == "refused"
    assert asked == []


async def test_a_model_that_is_down_is_the_error_the_plain_route_gives(
    live_api: AsyncClient, monkeypatch
) -> None:
    """Before any piece has been sent there is still a status code to say it with."""
    _answered_by(monkeypatch, _Writer(PIECES, fail_at=0))

    answered = await live_api.post("/chat/stream", json=QUESTION)

    assert answered.status_code == 500
    message = answered.json()["error"]["message"]
    assert "The local model is not answering" in message
    assert "Nothing was sent anywhere else" in message


async def test_a_model_that_stops_part_way_says_so_in_the_stream(
    live_api: AsyncClient, monkeypatch
) -> None:
    """The status line has gone out by then, so the failure is a frame. And there
    is no closing reply: half an answer must not be listed with sources under it."""
    _answered_by(monkeypatch, _Writer(PIECES, fail_at=2))

    frames = await _all_frames(live_api, QUESTION)

    assert [frame["type"] for frame in frames] == ["delta", "delta", "error"]
    assert "stopped part way" in frames[-1]["message"]
    assert "model runner has stopped" in frames[-1]["message"]


async def test_a_provider_that_cannot_stream_sends_its_answer_whole(
    live_api: AsyncClient, monkeypatch
) -> None:
    _answered_by(monkeypatch, _Whole())

    frames = await _all_frames(live_api, {**QUESTION, "provider": "gemini"})

    assert [frame["type"] for frame in frames] == ["delta", "done"]
    assert frames[0]["text"] == ANSWER
    assert frames[1]["reply"]["local"] is False


async def test_an_unknown_provider_is_refused_before_anything_is_built(
    live_api: AsyncClient, monkeypatch
) -> None:
    asked = _answered_by(monkeypatch, _Writer(PIECES))

    answered = await live_api.post("/chat/stream", json={**QUESTION, "provider": "somebody"})

    assert answered.status_code == 400
    assert asked == []


async def test_a_dock_that_closes_stops_the_model(live_api: AsyncClient, monkeypatch) -> None:
    """Closing the tab ends the request. The model must stop writing, and the
    database must still answer afterwards (§17: a cancelled request that was
    part-way through returning a connection poisons the next one to use it)."""
    never = asyncio.Event()
    writer = _Writer(PIECES, hold_last=never)
    _answered_by(monkeypatch, writer)

    async with live_api.stream("POST", "/chat/stream", json=QUESTION) as response:
        assert (await _Frames(response).next())["type"] == "delta"

    await asyncio.wait_for(writer.stopped.wait(), FRAME_TIMEOUT_S)
    assert (await live_api.get("/applications")).status_code == 200


async def test_a_cancelled_request_still_finishes_reading(
    committing_sessionmaker, monkeypatch
) -> None:
    """What the shield is for, shown by cancelling the route itself (§17).

    A cancel that lands while the context is being read must not interrupt
    the read or the session's return to the pool: a connection abandoned
    part-way through that is broken for whoever checks it out next. The read
    is made to wait, the request is cancelled, and the read is then let go.
    Without the shield the cancel reaches the read and it never finishes.
    """
    from sqlalchemy import text

    import packages.core.db as core_db
    from packages.core.schemas import ChatRequest

    monkeypatch.setattr(core_db, "get_sessionmaker", lambda: committing_sessionmaker)
    reading, go_on, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def slow(session: Any, question: str) -> Retrieval:
        reading.set()
        await go_on.wait()
        finished.set()
        return Retrieval(attempted=False)

    monkeypatch.setattr(chat_module, "retrieve", slow)
    _answered_by(monkeypatch, _Writer(PIECES))

    request = asyncio.create_task(
        stream_module.chat_stream(ChatRequest(message=QUESTION["message"]))
    )
    await asyncio.wait_for(reading.wait(), FRAME_TIMEOUT_S)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert not finished.is_set(), "the read was still waiting when the request was cancelled"

    go_on.set()
    await asyncio.wait_for(finished.wait(), FRAME_TIMEOUT_S)
    # Let the read's own task close its session before the pool is used again.
    for _ in range(20):
        await asyncio.sleep(0.01)
    async with committing_sessionmaker() as session:
        assert (await session.execute(text("select 1"))).scalar_one() == 1


def test_the_stream_holds_no_request_session() -> None:
    """§17, held in the source. A session that belongs to the request is closed
    when the request is, and a streaming request is closed by the reader leaving.
    Everything the answer needs is read, on a session of its own, before the
    first piece is sent. What the shield does is shown by the test above; this
    one holds that the route is still written that way."""
    route = inspect.signature(stream_module.chat_stream)
    assert "session" not in route.parameters
    assert "SessionDep" not in inspect.getsource(stream_module)

    assert "asyncio.shield(_prepared(body))" in inspect.getsource(stream_module.chat_stream)
