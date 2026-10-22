"""Explicit, offline context-packing seam; live routes do not enable it.

The caller supplies a model/template-specific local token counter. No guessed
characters-per-token ratio, implicit tokenizer download or model request.
"""

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel


class BudgetExceeded(ValueError):
    """Mandatory evidence cannot fit; ask no model rather than truncate it."""


@dataclass(frozen=True)
class PromptBudget:
    counter: Callable[[str, str], int]
    tokenizer_id: str
    context_tokens: int

    def __post_init__(self) -> None:
        if not self.tokenizer_id.strip() or self.context_tokens <= 0:
            raise ValueError("a named tokenizer and positive context cap are required")


class BudgetReport(BaseModel):
    tokenizer_id: str
    context_tokens: int
    answer_tokens: int
    safety_tokens: int
    initial_prompt_tokens: int
    prompt_tokens: int
    removed_excerpts: list[str]


def pack(
    *,
    system: str,
    render: Callable[[frozenset[str]], str],
    labels: list[str],
    budget: PromptBudget,
    answer_tokens: int = 600,
    safety_tokens: int = 128,
) -> tuple[str, BudgetReport]:
    """Drop lowest-ranked entire excerpts only; preserve every other field.

    The counter must include system, user, template and generation-prefix
    tokens. The reservation also leaves room for the answer and safety margin.
    """
    if answer_tokens < 0 or safety_tokens < 0 or len(set(labels)) != len(labels):
        raise ValueError("invalid reservation or duplicate labels")
    available = budget.context_tokens - answer_tokens - safety_tokens
    if available < 0:
        raise BudgetExceeded("answer and safety reservations exceed context")
    included = set(labels)
    removed: list[str] = []
    initial = None
    for iteration in range(len(labels) + 1):
        prompt = render(frozenset(included))
        count = budget.counter(system, prompt)
        if type(count) is not int or count < 0:
            raise ValueError("token counter must return a nonnegative integer")
        if initial is None:
            initial = count
        if count <= available:
            return prompt, BudgetReport(
                tokenizer_id=budget.tokenizer_id,
                context_tokens=budget.context_tokens,
                answer_tokens=answer_tokens,
                safety_tokens=safety_tokens,
                initial_prompt_tokens=initial,
                prompt_tokens=count,
                removed_excerpts=removed,
            )
        if iteration < len(labels):
            label = labels[-iteration - 1]
            included.remove(label)
            removed.append(label)
    raise BudgetExceeded("mandatory context does not fit; no evidence was silently truncated")
