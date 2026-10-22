"""Offline answer-support review; no automatic entailment or live interception.

Every nonblank answer line is a review unit, including headings. A reviewer
must judge *all* claims in that line. Rates are line support, not claim precision.
Quotes establish provenance; only a reader can establish that they entail a claim.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from apps.api.routers.chat import SYSTEM, Ready, cited_labels
from packages.core.schemas import ChatReply

Verdict = Literal["pending", "supported", "contradicted", "unsupported", "uncertain", "non_factual"]


class LineReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    verdict: Verdict = "pending"
    reviewer: Literal["owner", "model"] | None = None
    note: str = ""
    quotes: list[str] = Field(default_factory=list)


class AnswerReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    case_id: str = Field(min_length=1)
    system: str
    prompt: str
    answer: str
    model: str | None
    labels: list[str]
    snapshot_sha256: str
    lines: list[LineReview]

    def digest(self) -> str:
        frozen = self.model_dump(exclude={"lines", "snapshot_sha256"})
        return hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest()

    @model_validator(mode="after")
    def valid_review(self) -> Self:
        if self.snapshot_sha256 != self.digest():
            raise ValueError("frozen material changed")
        if [line.text for line in self.lines] != _lines(self.answer):
            raise ValueError("review must cover every nonblank answer line in order")
        for line in self.lines:
            if line.verdict == "pending":
                if line.reviewer or line.note or line.quotes:
                    raise ValueError("pending line contains a review")
                continue
            if not line.reviewer or not line.note.strip():
                raise ValueError("reviewer and explanation required")
            if any(not q.strip() or q not in self.prompt for q in line.quotes):
                raise ValueError("evidence must quote the frozen context exactly")
            if line.verdict in {"supported", "contradicted"} and not line.quotes:
                raise ValueError("supported/contradicted reviews require evidence")
            if line.verdict == "supported" and not cited_labels(line.text) <= set(self.labels):
                raise ValueError("a supported line cites an unsupplied source")
        return self


def _lines(answer: str) -> list[str]:
    return [line for line in answer.splitlines() if line.strip()]


def freeze(
    *, case_id: str, system: str, prompt: str, answer: str, model: str | None, labels: list[str]
) -> AnswerReview:
    """Freeze once; only the line review fields should subsequently be edited."""
    record = AnswerReview.model_construct(
        case_id=case_id,
        system=system,
        prompt=prompt,
        answer=answer,
        model=model,
        labels=sorted(set(labels)),
        snapshot_sha256="",
        lines=[LineReview(text=line) for line in _lines(answer)],
    )
    record.snapshot_sha256 = record.digest()
    return AnswerReview.model_validate(record.model_dump())


def capture(case_id: str, ready: Ready, reply: ChatReply) -> AnswerReview:
    """Use after prepare/complete/reply_for; capture no new model call or DB read.

    Includes sources from the prior turn only when actually in this prompt,
    matching reply_for's cited-label availability rule.
    """
    return freeze(
        case_id=case_id,
        system=SYSTEM,
        prompt=ready.prompt,
        answer=reply.reply,
        model=reply.model,
        labels=[p.label for p in ready.found.passages] + [p.label for p in ready.earlier],
    )


class SupportScore(BaseModel):
    cases: int
    empty_answers: int
    lines: int
    pending: int
    supported: int
    contradicted: int
    unsupported: int
    uncertain: int
    non_factual: int
    owner_reviewed: int
    model_reviewed: int
    complete: bool
    support_rate: float | None


def score(records: list[AnswerReview], *, provisional: bool = False) -> SupportScore:
    """Aggregate without printing private text. Uncertain is not supported."""
    if len({r.case_id for r in records}) != len(records):
        raise ValueError("duplicate case ids")
    # Revalidate mutable Pydantic objects too, not just files read by the CLI.
    records = [AnswerReview.model_validate(r.model_dump()) for r in records]
    lines = [line for record in records for line in record.lines]
    counts = {
        v: sum(line.verdict == v for line in lines)
        for v in ("pending", "supported", "contradicted", "unsupported", "uncertain", "non_factual")
    }
    if counts["pending"] and not provisional:
        raise ValueError("unfinished reviews; use --provisional to report coverage")
    factual = sum(counts[v] for v in ("supported", "contradicted", "unsupported", "uncertain"))
    return SupportScore(
        cases=len(records),
        empty_answers=sum(not r.lines for r in records),
        lines=len(lines),
        **counts,
        owner_reviewed=sum(line.reviewer == "owner" for line in lines),
        model_reviewed=sum(line.reviewer == "model" for line in lines),
        complete=bool(records) and not counts["pending"] and all(r.lines for r in records),
        support_rate=counts["supported"] / factual if factual else None,
    )
