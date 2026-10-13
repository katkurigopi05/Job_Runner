"""An answer Gemini was cut off in the middle of is not an answer.

Found on 2026-10-13 by tailoring one résumé for one posting and reading why
so little of it changed. Gemini's flash models think before they write, and
`maxOutputTokens` bounds the thinking and the answer together. Every caller
here means `max_tokens` as an answer budget (§7): the tailorer passes 300 to
keep a bullet bullet-sized. Measured on `gemini-3.8-flash` with the
tailorer's own prompt and a fixture bullet:

    cap 300    thought 288, wrote 8    finishReason MAX_TOKENS
               "Implemented a Python and PostgreSQL data processing pipeline"
    cap 4396   thought 839, wrote 17   finishReason STOP
               "… pipeline handling 12 million events a day."

Six samples with room thought 839 to 1,498 tokens and wrote 15 to 40. So at
300 every answer is cut off, and the provider returned the fragment as the
answer. The fabrication guard asks whether a rewrite says something the
résumé does not, and a bullet that stops half way says nothing new: "Built a
Python and PostgreSQL pipeline handling" passes it, with the metric gone.

This is §7's OpenRouter trap again (`REASONING_HEADROOM_TOKENS`), on the
provider the project ships with. Two changes, and the second is the one that
matters if the first is ever too small: the thinking gets room of its own,
and a response that says it was cut off is refused whatever text came with
it.

No test here reaches Gemini. The responses are the shapes it sent.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from packages.llm.provider import THINKING_HEADROOM_TOKENS, GeminiProvider, LLMError
from packages.tailor.guard import SourceCorpus
from packages.tailor.rewrite import tailor_bullet

RESUME = """Jane Doe — Backend Engineer

Acme Corp — Senior Backend Engineer, 2021 to 2024
Maintained the billing service and reduced invoice errors by 30%.
Built a Python and PostgreSQL pipeline handling 12 million events a day.

Skills: Python, PostgreSQL, Docker, Kafka
"""
BULLET = "Built a Python and PostgreSQL pipeline handling 12 million events a day."
#: Stops where the limit fell. The guard accepts it: nothing in it is new.
CUT_OFF = "Built a Python and PostgreSQL pipeline handling"
POSTING = "We need a backend engineer with Python and PostgreSQL experience."


def _candidate(finish: str, text: str | None) -> dict[str, Any]:
    content: dict[str, Any] = {"role": "model"}
    if text is not None:
        content["parts"] = [{"text": text}]
    return {"candidates": [{"content": content, "finishReason": finish}]}


@pytest.fixture
def gemini(monkeypatch):
    """A `GeminiProvider` whose one HTTP call is answered from `gemini.reply`."""
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    provider = GeminiProvider()
    seen: dict[str, Any] = {}

    class Response:
        status_code = 200
        headers: dict[str, str] = {}

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return dict(provider.reply)  # type: ignore[attr-defined]

    async def post(self, url, *, json, timeout):  # noqa: ANN001, ARG001
        seen["json"] = json
        return Response()

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    provider.reply = _candidate("STOP", "ok")  # type: ignore[attr-defined]
    provider.seen = seen  # type: ignore[attr-defined]
    return provider


async def test_the_answer_budget_is_not_spent_on_thinking(gemini) -> None:
    await gemini.complete("sys", "usr", max_tokens=300)

    asked = gemini.seen["json"]["generationConfig"]["maxOutputTokens"]
    assert asked == 300 + THINKING_HEADROOM_TOKENS


def test_the_room_covers_the_thinking_that_was_measured() -> None:
    """1,498 tokens was the most of six samples. OpenRouter's 1,024 would
    have cut four of them off."""
    assert THINKING_HEADROOM_TOKENS >= 2 * 1498


async def test_a_finished_answer_is_returned(gemini) -> None:
    gemini.reply = _candidate("STOP", BULLET)

    assert await gemini.complete("sys", "usr", max_tokens=300) == BULLET


async def test_an_answer_cut_off_at_the_limit_is_refused(gemini) -> None:
    """The text is there and reads like a sentence. It is still not what the
    model was going to say."""
    gemini.reply = _candidate("MAX_TOKENS", CUT_OFF)

    with pytest.raises(LLMError, match="cut off"):
        await gemini.complete("sys", "usr", max_tokens=300)


async def test_thinking_that_used_the_whole_budget_says_so(gemini) -> None:
    """No text at all: the limit fell before the first word. It used to read
    "Gemini returned no text: 'parts'", which is how a safety block reads."""
    gemini.reply = _candidate("MAX_TOKENS", None)

    with pytest.raises(LLMError, match="cut off") as refused:
        await gemini.complete("sys", "usr", max_tokens=300)

    assert "THINKING_HEADROOM_TOKENS" in str(refused.value)


async def test_a_response_with_no_text_still_says_why(gemini) -> None:
    gemini.reply = _candidate("SAFETY", None)

    with pytest.raises(LLMError, match="SAFETY"):
        await gemini.complete("sys", "usr")


async def test_no_candidate_at_all_is_still_an_error(gemini) -> None:
    gemini.reply = {"promptFeedback": {"blockReason": "OTHER"}}

    with pytest.raises(LLMError, match="no text"):
        await gemini.complete("sys", "usr")


async def test_the_refusal_does_not_quote_the_fragment(gemini) -> None:
    """A fragment of a rewritten bullet is résumé content (§10), and this
    message is logged."""
    gemini.reply = _candidate("MAX_TOKENS", CUT_OFF)

    with pytest.raises(LLMError) as refused:
        await gemini.complete("sys", "usr", max_tokens=300)

    assert "PostgreSQL" not in str(refused.value)


async def test_the_guard_alone_would_have_let_the_fragment_through() -> None:
    """Why the provider has to refuse it. Handed over as an answer, the
    fragment is accepted, and the bullet loses its number."""
    from packages.llm.provider import StubProvider

    rewrite = await tailor_bullet(
        StubProvider({"Built": CUT_OFF}), BULLET, POSTING, SourceCorpus.from_texts(RESUME)
    )

    assert rewrite.tailored == CUT_OFF
    assert "12 million" not in rewrite.tailored


async def test_a_cut_off_bullet_keeps_the_resumes_own(gemini) -> None:
    """The same fragment through the provider. The bullet is the résumé's,
    and it is counted as a call that got no answer, not as a rewrite and not
    as something the guard refused."""
    gemini.reply = _candidate("MAX_TOKENS", CUT_OFF)

    rewrite = await tailor_bullet(gemini, BULLET, POSTING, SourceCorpus.from_texts(RESUME))

    assert rewrite.tailored == BULLET
    assert rewrite.provider_failed
