"""Classify an inbound recruiter message.

Two classifiers behind one function:

- `RuleClassifier` (default) — ordered pattern rules. Deterministic, offline,
  and auditable: every verdict comes with the phrase that produced it, so a
  wrong answer points straight at the rule to fix.
- `LLMClassifier` — routes through the provider abstraction, which CLAUDE.md
  §7 assigns to a local Ollama model.

The rule classifier runs first even when a model is configured, and the model
is only consulted when the rules abstain. Rejections and interview invitations
use near-boilerplate language, so rules handle the common cases exactly, and
the model is spent on the genuinely ambiguous remainder.

Order matters more than cleverness here. "Unfortunately we have decided to
move forward with other candidates" contains "move forward", which reads as
positive in isolation — so rejection patterns are checked before interview
ones, and the tests pin that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import structlog

from packages.core.enums import Classification

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ClassificationResult:
    classification: Classification
    #: What in the text produced this verdict. Empty when nothing matched.
    evidence: str = ""
    #: Rules are certain by construction; a model's guess is not.
    confident: bool = True

    @property
    def abstained(self) -> bool:
        return self.classification is Classification.NOISE and not self.evidence


#: (classification, pattern) in priority order. First match wins, so anything
#: whose language overlaps a later category must appear before it.
RULES: tuple[tuple[Classification, re.Pattern[str]], ...] = (
    # Rejections first: they borrow the vocabulary of every other category.
    (
        Classification.REJECTION,
        re.compile(
            r"\b(?:unfortunately|regret to inform|we regret|not (?:be )?"
            r"(?:moving|proceeding) forward|decided (?:to )?(?:move forward |proceed )?"
            r"with other candidates|will not be (?:moving|proceeding)|"
            r"no longer under consideration|not selected|"
            r"pursue other candidates|position has been filled|"
            r"decided not to move forward)\b",
            re.I,
        ),
    ),
    (
        Classification.OFFER,
        re.compile(
            r"\b(?:offer of employment|pleased to offer|formal offer|"
            r"extend(?:ing)? (?:you )?an offer|offer letter)\b",
            re.I,
        ),
    ),
    (
        Classification.OTP,
        re.compile(
            r"\b(?:verification code|security code|one[- ]time (?:code|password)|"
            r"confirm your email|your code is|passcode)\b",
            re.I,
        ),
    ),
    (
        Classification.INTERVIEW,
        re.compile(
            r"\b(?:schedule (?:a |an )?(?:call|chat|interview|time)|"
            r"invite you to interview|would like to (?:speak|chat|talk|meet)|"
            r"set up (?:a |an )?(?:call|time|chat|interview)|"
            r"book (?:a |some )?time|availability (?:for|next)|"
            r"phone screen|technical interview|onsite interview|"
            r"next steps? (?:is|would be) (?:a|an)? ?(?:call|interview))\b",
            re.I,
        ),
    ),
    (
        Classification.INFO_REQUEST,
        re.compile(
            r"\b(?:could you (?:please )?(?:send|provide|share|confirm)|"
            r"we need (?:you to |your )|please (?:complete|fill out|provide|send|upload)|"
            r"additional information|work authorization|"
            r"required documents?|background check form)\b",
            re.I,
        ),
    ),
    (
        Classification.ACKNOWLEDGEMENT,
        re.compile(
            r"\b(?:we (?:have )?received your application|thank you for applying|"
            r"thanks for applying|application (?:has been )?received|"
            r"your application (?:to|for) .{0,60} (?:has been|was) received|"
            r"we are reviewing (?:your )?application)\b",
            re.I,
        ),
    ),
    (
        Classification.NOISE,
        re.compile(
            r"\b(?:unsubscribe from|newsletter|webinar|"
            r"limited time offer|black friday|"
            r"job alert|new jobs? matching|recommended jobs?)\b",
            re.I,
        ),
    ),
)


class RuleClassifier:
    """Ordered pattern matching. Deterministic and auditable."""

    name = "rules"

    def classify(self, subject: str, body: str) -> ClassificationResult:
        # Subject first: it carries the decision in most recruiter mail, and
        # bodies often quote an earlier thread that would confuse the match.
        for text in (subject or "", body or ""):
            if not text.strip():
                continue
            for classification, pattern in RULES:
                match = pattern.search(text)
                if match:
                    return ClassificationResult(
                        classification=classification, evidence=match.group(0)
                    )

        # Nothing matched. Abstaining is different from deciding it is noise,
        # which is why `abstained` exists — it is what hands the message to a
        # model, or to the owner.
        return ClassificationResult(classification=Classification.NOISE, evidence="")


CLASSIFY_SYSTEM_PROMPT = """
Classify a recruiter email into exactly one of:
interview, rejection, offer, info_request, acknowledgement, otp, noise.

Answer with the single word and nothing else.

A rejection is any message declining the application, however politely worded.
An acknowledgement only confirms receipt. An interview proposes speaking. Do
not treat a polite rejection as an interview because it mentions next steps.
""".strip()


class LLMClassifier:
    """Falls back to a model when the rules abstain."""

    name = "llm"

    def __init__(self, provider: object, rules: RuleClassifier | None = None) -> None:
        self.provider = provider
        self.rules = rules or RuleClassifier()

    async def classify(self, subject: str, body: str) -> ClassificationResult:
        rule_result = self.rules.classify(subject, body)
        if not rule_result.abstained:
            return rule_result

        prompt = f"Subject: {subject}\n\n{body[:4000]}"
        try:
            raw = await self.provider.complete(  # type: ignore[attr-defined]
                CLASSIFY_SYSTEM_PROMPT, prompt, max_tokens=10
            )
        except Exception as exc:  # noqa: BLE001 - a model outage is not fatal
            log.warning("classifier_provider_failed", error=type(exc).__name__)
            return rule_result

        answer = (raw or "").strip().lower().split()
        if not answer:
            return rule_result

        try:
            classification = Classification(answer[0])
        except ValueError:
            log.info("classifier_unusable_answer")
            return rule_result

        return ClassificationResult(
            classification=classification, evidence="model", confident=False
        )


def classify(subject: str, body: str) -> ClassificationResult:
    """Rule-based classification. The offline default."""
    return RuleClassifier().classify(subject, body)
