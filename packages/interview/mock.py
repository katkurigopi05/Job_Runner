"""A single question, answer, follow-up, answer and rubric feedback."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from packages.interview.plan import RUBRIC
from packages.interview.research import interview_provider, structured
from packages.llm.audit import AuditUnavailable
from packages.llm.provider import LLMError
from packages.llm.quota import QuotaExceeded


class MockInput(BaseModel):
    action: Literal["start", "answer"]
    answer: str = Field(default="", max_length=3000)
    turn: Literal["question", "followup"] | None = None
    use_kaggle: bool = False


class Followup(BaseModel):
    question: str = Field(min_length=1, max_length=300)


class Feedback(BaseModel):
    relevance: str = Field(min_length=1, max_length=400)
    evidence: str = Field(min_length=1, max_length=400)
    reasoning: str = Field(min_length=1, max_length=400)


async def advance(
    progress: dict[str, Any],
    body: MockInput,
    *,
    fixture: bool,
    share_mail: bool = False,
) -> dict[str, Any]:
    previous = progress.get("mock", {})
    if body.action == "start":
        return {
            "stage": "question",
            "question": progress["plan"]["questions"][0],
            "rubric": list(RUBRIC),
        }
    stage = previous.get("stage")
    if stage not in {"question", "followup"} or stage != body.turn or not body.answer.strip():
        raise ValueError("Answer the current mock question first.")
    answer = body.answer.strip()
    result = {**previous}
    trace: dict[str, Any] = {}
    use_kaggle = body.use_kaggle and not fixture
    mode = "fixture" if fixture else "local"
    model_result = None
    if use_kaggle:
        try:
            provider = interview_provider()
            prompt = {
                "task": (
                    "Ask one relevant follow-up about the user's answer."
                    if stage == "question"
                    else "Give concise qualitative feedback for each rubric criterion. "
                    "Cite only what the user says and distinguish verified evidence from claims."
                ),
                "rubric": list(RUBRIC),
                "question": (
                    previous["question"][:300]
                    if share_mail
                    else "Describe your approach to a technical interview exercise."
                ),
                "answer": (answer[:1000] if stage == "question" else previous["answer"][:1000]),
                "followup": previous.get("followup", ""),
                "followup_answer": answer[:1000] if stage == "followup" else "",
            }
            model_result = await structured(
                provider, prompt, Followup if stage == "question" else Feedback, trace
            )
            mode = "kaggle"
        except (LLMError, AuditUnavailable, QuotaExceeded) as exc:
            mode = "local fallback"
            trace["outcome"] = type(exc).__name__
    if stage == "question":
        followup = (
            model_result.question
            if isinstance(model_result, Followup)
            else "Pick one decision from your answer. What alternative did you consider, "
            "and how would you test your choice?"
        )
        result.update(stage="followup", answer=answer, followup=followup)
    else:
        if isinstance(model_result, Feedback):
            feedback = model_result
        else:
            feedback = Feedback(
                relevance=(
                    "Lead with a direct answer, then connect the example back to the question."
                    if len(previous["answer"].split()) < 20
                    else "Check that your first two sentences directly address the question."
                ),
                evidence=("Name the artifact and your contribution. Use a verifiable result."),
                reasoning=(
                    "Add an alternative, a reason for your choice, and one test you would run."
                    if len(answer.split()) < 20
                    else "Explain what outcome would cause you to change your approach."
                ),
            )
        result.update(
            stage="feedback",
            followup_answer=answer,
            feedback=dict(zip(RUBRIC, feedback.model_dump().values(), strict=True)),
        )
    return {**result, "mode": mode, "trace": trace}
