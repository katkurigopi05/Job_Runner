"""`POST /chat/stream`: the assistant's answer, sent as the model writes it.

`POST /chat` sends nothing until the model has finished. On the owner's
machine that is about 5 s reading the prompt and then the answer at about 12
tokens a second (CLAUDE.md §14), so the dock sat empty for the whole of it.
Here each piece is sent as it is written, and the stream closes with the same
reply `/chat` returns.

Three things this route has to keep:

- **The rules before the model are `/chat`'s own.** `prepare`, `provider_for`
  and `did_not_answer` are imported from it, not repeated: the §2.2 refusal,
  the crawl command, the provider checks and the no-fallback errors cannot
  drift between the two routes.
- **Citations come last.** They are read out of the finished text
  (`cited_labels`), so they arrive in the closing frame with everything else
  the dock lists under an answer.
- **It holds no request session** (§17). A reader who leaves cancels the
  request. Everything is read before the first piece is sent, on a session
  opened and closed inside one shielded awaitable.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from apps.api.routers.chat import (
    ANSWER_TEMPERATURE,
    ANSWER_TOKENS,
    SYSTEM,
    Ready,
    did_not_answer,
    prepare,
    provider_for,
    reply_for,
)
from packages.core import db as core_db
from packages.core.schemas import ChatReply, ChatRequest
from packages.llm.provider import LLMError, stream_text

router = APIRouter(prefix="/chat", tags=["chat"])

#: The stream is one JSON object to a line.
STREAM_MEDIA_TYPE = "application/x-ndjson"

#: `no-transform` is the load-bearing one (§17). Next's proxy re-encodes a
#: proxied body and a compressor buffers, so without it the dock connects and
#: then shows nothing until the answer is complete.
STREAM_HEADERS = {
    "Cache-Control": "no-store, no-transform",
    "X-Accel-Buffering": "no",
}


def _frame(kind: str, **fields: Any) -> str:
    return json.dumps({"type": kind, **fields}) + "\n"


async def _prepared(body: ChatRequest) -> ChatReply | Ready:
    """`prepare` on a session of its own, opened and closed in one awaitable.

    So the caller can shield all of it. §17: a reader who leaves cancels the
    request, and a cancel that lands while a connection is being returned to
    the pool leaves it broken for whoever checks it out next.
    """
    async with core_db.get_sessionmaker()() as session:
        return await prepare(body, session)


async def _unwritten(reply: ChatReply) -> AsyncIterator[str]:
    """An answer no model wrote: the refusal, or the crawl command."""
    yield _frame("done", reply=reply.model_dump(mode="json", by_alias=True))


async def _written(
    first: str, pieces: AsyncIterator[str], ready: Ready, model: str | None
) -> AsyncIterator[str]:
    """Each piece as it is written, then the reply `/chat` would have returned.

    Citations are read out of the finished text, so they and the rest of what
    the dock lists arrive in the closing frame. A model that fails part way
    gets no closing frame: half an answer must not be listed with sources
    under it.
    """
    written = [first]
    try:
        if first:
            yield _frame("delta", text=first)
        async for piece in pieces:
            written.append(piece)
            yield _frame("delta", text=piece)
    except LLMError as exc:
        yield _frame(
            "error",
            message=(
                f"{model or ready.selected} stopped part way through the answer ({exc}). "
                "Nothing fell back to another model. Ask again."
            ),
        )
        return
    finally:
        # A reader who leaves must also stop the model writing.
        await pieces.aclose()  # type: ignore[attr-defined]
    reply = reply_for(ready, model, "".join(written))
    yield _frame("done", reply=reply.model_dump(mode="json", by_alias=True))


@router.post("/stream")
async def chat_stream(body: ChatRequest) -> StreamingResponse:
    """`POST /chat`, with the answer sent as the model writes it.

    One JSON object to a line: `delta` frames carrying text, then a `done`
    frame carrying the same reply `/chat` returns. Every rule that runs before
    the model is the same code as `/chat`.

    The first piece is waited for before the response starts. Until then there
    is still a status code to answer with, so a model that is down is the same
    error `/chat` gives and the dock handles it the same way. After that a
    failure can only be an `error` frame.

    This route takes no request session. Everything is read before the first
    piece is sent, on a session that is closed again inside the shield.
    """
    ready = await asyncio.shield(_prepared(body))
    if isinstance(ready, ChatReply):
        return StreamingResponse(
            _unwritten(ready), media_type=STREAM_MEDIA_TYPE, headers=STREAM_HEADERS
        )
    try:
        provider, model = provider_for(ready.selected)
        pieces = stream_text(
            provider, SYSTEM, ready.prompt, max_tokens=ANSWER_TOKENS, temperature=ANSWER_TEMPERATURE
        )
        first = await anext(pieces, "")
    except LLMError as exc:
        raise did_not_answer(ready.selected, exc) from exc
    return StreamingResponse(
        _written(first, pieces, ready, model),
        media_type=STREAM_MEDIA_TYPE,
        headers=STREAM_HEADERS,
    )
