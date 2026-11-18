"""`complete_structured` finds the JSON, validates it, and asks again with the exact error.

Offline: a scripted provider that answers from a list, and `StubProvider`.
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, Field, RootModel

from packages.llm.provider import LLMError, StubProvider
from packages.llm.structured import (
    MAX_ECHOED_CHARS,
    StructuredOutputError,
    complete_structured,
    extract_json,
)


class Match(BaseModel):
    term: str
    score: int = Field(ge=0, le=3)


class Matches(RootModel[list[Match]]):
    pass


VALID = '{"term": "FastAPI", "score": 2}'


class Scripted:
    """Answers from a list, in order, and records what each call asked."""

    name = "scripted"

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, object]] = []

    async def complete(
        self, system: str, user: str, *, max_tokens: int = 1024, temperature: float = 0.2
    ) -> str:
        self.calls.append(
            {"system": system, "user": user, "max_tokens": max_tokens, "temperature": temperature}
        )
        return self.answers.pop(0)

    async def complete_json(self, system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        raise AssertionError("complete_structured must not depend on a JSON mode")


class Down(Scripted):
    async def complete(self, system: str, user: str, **_: object) -> str:
        self.calls.append({"system": system, "user": user})
        raise LLMError("connection refused")


# --------------------------------------------------------------------------
# Finding the JSON
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        VALID,
        f"```json\n{VALID}\n```",
        f"Here is the result:\n```\n{VALID}\n```\nLet me know if you need more.",
        f"Sure! The answer is {VALID} and that is all.",
        f"Answer [v2]: {VALID}",
        f"{VALID}\nNote: scored on a 0-3 scale.",
    ],
)
def test_the_json_is_found_wherever_the_model_put_it(answer: str) -> None:
    assert extract_json(answer) == {"term": "FastAPI", "score": 2}


def test_an_array_is_found_inside_prose() -> None:
    answer = 'Two matches:\n[{"term": "Go", "score": 1}, {"term": "Redis", "score": 3}]'
    assert extract_json(answer) == [{"term": "Go", "score": 1}, {"term": "Redis", "score": 3}]


@pytest.mark.parametrize("answer", ["", "   ", "No JSON here.", '{"score": NaN}'])
def test_no_json_is_an_error(answer: str) -> None:
    with pytest.raises(ValueError):
        extract_json(answer)


def test_malformed_json_reports_where_it_broke() -> None:
    with pytest.raises(json.JSONDecodeError):
        extract_json('Result: {"term": "FastAPI" "score": 2}')


# --------------------------------------------------------------------------
# One call when the first answer is good
# --------------------------------------------------------------------------


async def test_a_valid_object_validates_on_the_first_call() -> None:
    provider = Scripted(f"```json\n{VALID}\n```")

    result = await complete_structured(provider, "Score FastAPI.", Match, system="You grade.")

    assert result == Match(term="FastAPI", score=2)
    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert call["user"] == "Score FastAPI."
    assert call["temperature"] == 0.0
    assert str(call["system"]).startswith("You grade.\n\n")
    assert '"score"' in str(call["system"])  # the schema travels with the instruction


async def test_a_valid_array_validates_into_a_root_model() -> None:
    provider = Scripted('[{"term": "Go", "score": 1}]')

    result = await complete_structured(provider, "Score them.", Matches)

    assert result.root == [Match(term="Go", score=1)]


async def test_the_stub_provider_works_unchanged() -> None:
    stub = StubProvider({"Score FastAPI": VALID})

    assert await complete_structured(stub, "Score FastAPI.", Match) == Match(
        term="FastAPI", score=2
    )
    assert stub.temperatures == [0.0]


# --------------------------------------------------------------------------
# Asking again with the error
# --------------------------------------------------------------------------


async def test_malformed_json_is_fed_back_and_the_retry_succeeds() -> None:
    broken = '{"term": "FastAPI", "score": 2'
    provider = Scripted(broken, VALID)

    result = await complete_structured(provider, "Score FastAPI.", Match)

    assert result == Match(term="FastAPI", score=2)
    assert len(provider.calls) == 2
    retry = str(provider.calls[1]["user"])
    assert retry.startswith("Score FastAPI.")
    assert broken in retry
    assert "It was not valid JSON" in retry
    assert "line 1 column" in retry  # the decoder's own position


async def test_a_validation_error_is_fed_back_field_by_field() -> None:
    provider = Scripted('{"term": "FastAPI", "score": 7}', '{"term": "FastAPI", "score": 3}')

    result = await complete_structured(provider, "Score FastAPI.", Match)

    assert result.score == 3
    retry = str(provider.calls[1]["user"])
    assert "It did not validate against the schema" in retry
    assert "- score: Input should be less than or equal to 3" in retry


async def test_a_missing_field_is_named() -> None:
    provider = Scripted('{"score": 1}', VALID)

    await complete_structured(provider, "Score FastAPI.", Match)

    assert "- term: Field required" in str(provider.calls[1]["user"])


async def test_each_retry_quotes_only_the_previous_answer() -> None:
    provider = Scripted("first", "second", VALID)

    await complete_structured(provider, "Score FastAPI.", Match)

    third = str(provider.calls[2]["user"])
    assert "second" in third
    assert "first" not in third


async def test_a_long_answer_is_quoted_back_in_part() -> None:
    provider = Scripted("x" * (MAX_ECHOED_CHARS * 3), VALID)

    await complete_structured(provider, "Score FastAPI.", Match)

    assert "x" * (MAX_ECHOED_CHARS + 1) not in str(provider.calls[1]["user"])


# --------------------------------------------------------------------------
# Giving up
# --------------------------------------------------------------------------


async def test_exhausted_retries_raise_a_typed_error() -> None:
    provider = Scripted("not json", '{"term": "Go"}', '{"term": "Go", "score": -1}')

    with pytest.raises(StructuredOutputError) as raised:
        await complete_structured(provider, "Score Go.", Match)

    error = raised.value
    assert isinstance(error, LLMError)  # existing provider-failure fallbacks catch it
    assert error.attempts == 3
    assert error.schema == "Match"
    assert error.errors[0].startswith("It was not valid JSON")
    assert "score: Field required" in error.errors[1]
    assert "greater than or equal to 0" in error.errors[2]
    assert len(provider.calls) == 3


async def test_the_stub_without_a_canned_answer_exhausts_cleanly() -> None:
    stub = StubProvider()

    with pytest.raises(StructuredOutputError):
        await complete_structured(stub, "Score FastAPI.", Match, max_retries=1)

    assert len(stub.calls) == 2


async def test_no_retries_means_one_call() -> None:
    provider = Scripted("not json")

    with pytest.raises(StructuredOutputError) as raised:
        await complete_structured(provider, "Score Go.", Match, max_retries=0)

    assert raised.value.attempts == 1
    assert len(provider.calls) == 1


async def test_a_provider_failure_is_not_retried_here() -> None:
    provider = Down()

    with pytest.raises(LLMError) as raised:
        await complete_structured(provider, "Score Go.", Match)

    assert not isinstance(raised.value, StructuredOutputError)
    assert len(provider.calls) == 1


async def test_negative_retries_are_refused() -> None:
    with pytest.raises(ValueError):
        await complete_structured(Scripted(), "Score Go.", Match, max_retries=-1)
