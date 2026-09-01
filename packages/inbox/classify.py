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
from typing import Protocol

import structlog

from packages.core.enums import Classification
from packages.llm.prompts import CLASSIFY_SYSTEM
from packages.llm.router import temperature_for

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ClassificationResult:
    classification: Classification
    #: What in the text produced this verdict. Empty when nothing matched.
    evidence: str = ""
    #: Rules are certain by construction; a model's guess is not.
    confident: bool = True
    #: How decisively this tier chose, when it can say. Naive Bayes reports the
    #: per-token log-odds gap between its best label and the runner-up; the
    #: rules leave it at 0.0 because an exact match has no runner-up to beat.
    #: Divided by token count so a long email and a short one are comparable —
    #: an unnormalized total grows with length and would call every long
    #: message confident.
    margin: float = 0.0

    @property
    def abstained(self) -> bool:
        return self.classification is Classification.NOISE and not self.evidence


#: (classification, pattern) in priority order. First match wins, so anything
#: whose language overlaps a later category must appear before it.
RULES: tuple[tuple[Classification, re.Pattern[str]], ...] = (
    # Rejections first: they borrow the vocabulary of every other category.
    (
        Classification.REJECTION,
        # Widened after measuring it: the original caught 2 of 14 realistic
        # phrasings. It was written beside the fixtures that exercise it, so
        # it matched "with other candidates" and missed "with another
        # candidate" — the same sentence as recruiters write it. Rejection is
        # the category where a miss costs most: the application sits in the
        # tracker as live, and the owner waits on a panel nobody will book.
        #
        # Widening the *first* rule is where precision goes to die, so the
        # alternatives all name an outcome ("not successful", "has been
        # filled") or somebody else getting it ("forward with another"), never
        # a bare verb. "Moving forward with your application" is an interview
        # and must stay one; only "forward with another/other/a different"
        # reads as a refusal.
        re.compile(
            r"(?:"
            r"\bunfortunately\b|\bregret to inform\b|\bwe regret\b|"
            r"\b(?:will |wo)?n[o'\u2019]?t be (?:moving forward|going forward|proceeding|"
            r"progressing|advancing|continuing)\b|"
            r"\bnot be (?:moving|going) forward\b|"
            r"\bnot be (?:proceeding|progressing|advancing|continuing)\b|"
            r"\bdecided not to (?:move forward|proceed|continue|advance|progress)\b|"
            r"\bunable to (?:move forward|proceed|progress)\b|"
            r"\bnot (?:be )?(?:moving|proceeding) forward\b|"
            r"\bno longer under consideration\b|\bnot (?:been )?selected\b|"
            r"\b(?:was|were) not successful\b|\bnot successful\b|"
            r"\b(?:position|role|vacancy) has been filled\b|"
            r"\bfilled (?:the|this) (?:position|role)\b|"
            r"\bdifferent direction\b|"
            r"\b(?:move|moving|moved|proceed|proceeding|go|going|continue|continuing) "
            r"forward with (?:another|other|a different|different|candidates whose|"
            r"applicants whose)\b|"
            r"\b(?:pursue|pursuing|considering) other (?:candidates|applicants)\b|"
            r"\b(?:selected|chosen|chose|hired) (?:another|a different)\b|"
            r"\bproceed(?:ing)? with (?:another|a different)\b|"
            r"\bwith other candidates\b"
            r")",
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


#: Defined in packages/llm/prompts.py — see the note there on versioning.
CLASSIFY_SYSTEM_PROMPT = CLASSIFY_SYSTEM.text


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
                CLASSIFY_SYSTEM_PROMPT,
                prompt,
                max_tokens=10,
                temperature=temperature_for("classify_inbound_email"),
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


#: Below this, a Naive Bayes verdict is treated as a shrug and handed on.
#:
#: Calibrated on the 30 labeled messages behind Gate 6: every message Bayes got
#: *wrong* scored at or below 0.190 per token, and the highest was 0.190, so
#: 0.2 sits just above all three mistakes. Correct verdicts run from 0.082 to a
#: median of 0.410, which means the gate also defers some it would have gotten
#: right. That is the cheap direction to be wrong in — a deferred correct
#: answer costs a model call, a kept wrong one silently mis-files a rejection.
#:
#: The realistic rejection in `tests/test_inbox.py::REALISTIC_REJECTION` is why
#: this gate is not decoration. Bayes reads it as **interview** at 0.073 — the
#: quoted thread carries the applicant's own "excited… interview panel", and
#: the evidence tokens come back as 'technical love up speak'. Kept, that files
#: a rejection as an interview, which is worse than any abstention: the owner
#: believes they are waiting on a panel that will never be scheduled. Deferred,
#: the model reads it correctly.
#:
#: Calibrated on fixtures, so provisional in exactly the way CLAUDE.md §15
#: describes. Re-derive it once real mail exists; the numbers above are the
#: procedure, not a constant of nature.
BAYES_MIN_MARGIN = 0.2


class SupportsClassify(Protocol):
    """Any tier that turns a message into a verdict.

    Declared rather than typing the parameter `object` and silencing the
    attribute error: `object` made the call return `Any`, and an `Any` flowing
    out of a function annotated `-> ClassificationResult` is exactly the hole
    mypy's `no-any-return` exists to catch. A Protocol keeps the tier
    swappable — `train_from_corpus()` today, a model fitted on real mail later
    — without giving up the return type on the way through.
    """

    def classify(self, subject: str, body: str) -> ClassificationResult: ...


async def classify_message(
    subject: str,
    body: str,
    *,
    provider: object | None = None,
    bayes: SupportsClassify | None = None,
) -> ClassificationResult:
    """Rules, then Naive Bayes, then a model — first tier that commits wins.

    `classify()` alone is why a real rejection reads as noise. Its patterns
    match the phrasings they were written beside: the fixture says "with other
    candidates" and matches, while "with another candidate" — the same
    sentence, as recruiters actually write it — does not, and the rules abstain
    on the whole message. An abstention then *becomes* `noise`, so the
    application's status never moves and the owner sees it waiting forever.

    The order is measured, not assumed. On Gate 6's 30: rules 29/30 with one
    abstention, Bayes 27/30 on its own. So Bayes must never run instead of the
    rules — only after them, where it resolved both abstentions tested,
    including a realistically messy rejection carrying a quoted thread, a
    signature block and an unsubscribe footer.

    Each tier may decline. The rules abstain by construction. Bayes cannot —
    it takes an argmax — so `BAYES_MIN_MARGIN` makes it able to, which is what
    keeps the model tier reachable rather than decorative.

    Degrades rather than fails: no provider, or a provider that is down, leaves
    the best verdict so far. A model outage must not turn into a mis-filed
    rejection.
    """
    rules_result = RuleClassifier().classify(subject, body)
    if not rules_result.abstained:
        return rules_result

    if bayes is None:
        bayes = _seed_bayes()
    if bayes is not None:
        guess = bayes.classify(subject, body)
        if not guess.abstained and guess.margin >= BAYES_MIN_MARGIN:
            log.info("classified_by_bayes", margin=round(guess.margin, 3))
            return guess

    if provider is not None:
        model_result = await LLMClassifier(provider).classify(subject, body)
        if not model_result.abstained:
            return model_result

    return rules_result


_SEED_BAYES: SupportsClassify | None = None
_SEED_BAYES_FAILED = False


def _seed_bayes() -> SupportsClassify | None:
    """The corpus-trained classifier, fitted once.

    Training is pure CPU over a few dozen short emails, but it happens on every
    inbound message otherwise, and an IMAP poll delivers them in batches.
    """
    global _SEED_BAYES, _SEED_BAYES_FAILED
    if _SEED_BAYES is None and not _SEED_BAYES_FAILED:
        try:
            from packages.inbox.bayes import train_from_corpus

            _SEED_BAYES = train_from_corpus()
        except Exception:  # noqa: BLE001 - a bad corpus must not stop routing
            log.warning("bayes_unavailable")
            _SEED_BAYES_FAILED = True
    return _SEED_BAYES
