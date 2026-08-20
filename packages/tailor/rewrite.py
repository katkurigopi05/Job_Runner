"""Résumé tailoring — rewrite bullets against a job description.

The model is not trusted. Every rewrite it returns is run through the
fabrication guard, and anything that does not trace to the source is
**discarded in favour of the original bullet**. A model that invents a metric
does not get its output softened or flagged for later; it gets ignored.

That fallback is what makes this safe to run with any provider, including a
local one the owner swaps in. The guard, not the prompt, is the guarantee —
prompts are advisory and models disregard them.
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel, Field

from packages.llm.prompts import TAILOR_SYSTEM
from packages.llm.provider import LLMProvider
from packages.llm.router import temperature_for
from packages.tailor.guard import _COMMON_WORDS, GuardReport, SourceCorpus, check, normalize

log = structlog.get_logger(__name__)

#: Defined in packages/llm/prompts.py so the audit trail can name the
#: version that produced a given rewrite. Edit it there, and bump.
SYSTEM_PROMPT = TAILOR_SYSTEM.text

#: A rewrite far longer than its source is usually padding, and padding is
#: where invented detail hides.
MAX_GROWTH_RATIO = 2.0

#: How much of the original's vocabulary a rewrite must retain.
#:
#: This closes a gap the entity guard cannot: fabrication written entirely in
#: ordinary lowercase words carries no proper noun, acronym, number, or year,
#: so it traces to nothing and is flagged by nothing. A rewrite that keeps
#: almost none of the original's words is not a rewrite — it is a different
#: sentence, and §2.1 does not license replacing a bullet with a new claim.
#:
#: Set low because re-emphasis is explicitly allowed. The cost of rejecting a
#: legitimate rewrite is falling back to the source, which is always safe.
MIN_VOCABULARY_OVERLAP = 0.35


class BulletRewrite(BaseModel):
    """One bullet, before and after, with the reason if it was rejected."""

    original: str
    tailored: str
    changed: bool = False
    #: Set when the model's attempt was discarded.
    rejected_reason: str | None = None
    entities_checked: int = 0

    @property
    def used_fallback(self) -> bool:
        return self.rejected_reason is not None


class TailorResult(BaseModel):
    bullets: list[BulletRewrite] = Field(default_factory=list)
    rejected: int = 0

    @property
    def tailored_lines(self) -> list[str]:
        return [b.tailored for b in self.bullets]

    @property
    def changed_count(self) -> int:
        return sum(1 for b in self.bullets if b.changed)


def _user_prompt(bullet: str, job_description: str) -> str:
    return (
        f"Job description:\n{job_description.strip()[:4000]}\n\n"
        f"Original bullet:\n{bullet.strip()}\n\n"
        "Rewritten bullet:"
    )


def _clean(candidate: str) -> str:
    """Strip the wrappers models add despite being told not to."""
    text = candidate.strip()
    for prefix in ("Rewritten bullet:", "Bullet:", "-", "*", "•"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def _content_words(text: str) -> set[str]:
    """Meaning-bearing words, ignoring grammar."""
    words = {normalize(w) for w in text.split()}
    return {w for w in words if w and w not in _COMMON_WORDS}


def vocabulary_overlap(original: str, candidate: str) -> float:
    """Share of the original's content words the candidate retains."""
    source_words = _content_words(original)
    if not source_words:
        return 1.0
    return len(source_words & _content_words(candidate)) / len(source_words)


def vet(
    original: str, candidate: str, corpus: SourceCorpus
) -> tuple[bool, str | None, GuardReport]:
    """Decide whether a rewrite may be used.

    Rejection reasons, in order of how often they matter: fabricated content,
    a bullet replaced wholesale rather than rewritten, implausible growth, or
    an empty answer.

    The fabrication check is scoped to the entry the original bullet came
    from. Checking against the whole résumé would accept a rewrite that moved
    one employer's metric onto another employer's bullet — every fact true
    somewhere, the sentence false where it stands.
    """
    report = check(candidate, corpus, scope=corpus.locate(original))

    if not candidate:
        return False, "model returned nothing", report
    if not report.ok:
        return False, report.summary(), report

    overlap = vocabulary_overlap(original, candidate)
    if overlap < MIN_VOCABULARY_OVERLAP:
        return (
            False,
            f"keeps only {overlap:.0%} of the original's wording; that is a "
            "replacement, not a rewrite",
            report,
        )

    if len(candidate) > max(len(original), 40) * MAX_GROWTH_RATIO:
        return False, "rewrite is disproportionately longer than the source", report

    return True, None, report


async def tailor_bullet(
    provider: LLMProvider,
    bullet: str,
    job_description: str,
    corpus: SourceCorpus,
) -> BulletRewrite:
    """Rewrite one bullet, falling back to the original if it does not vet."""
    try:
        raw = await provider.complete(
            SYSTEM_PROMPT,
            _user_prompt(bullet, job_description),
            max_tokens=300,
            temperature=temperature_for("tailor_resume"),
        )
    except Exception as exc:  # noqa: BLE001 - a provider failure is a fallback
        log.warning("tailor_provider_failed", error=type(exc).__name__)
        return BulletRewrite(
            original=bullet,
            tailored=bullet,
            rejected_reason=f"provider error: {type(exc).__name__}",
        )

    candidate = _clean(raw)
    accepted, reason, report = vet(bullet, candidate, corpus)

    if not accepted:
        # Never log the candidate itself — it may carry résumé content.
        log.info("tailor_rejected", reason=reason, violations=len(report.violations))
        return BulletRewrite(
            original=bullet,
            tailored=bullet,
            rejected_reason=reason,
            entities_checked=report.checked,
        )

    return BulletRewrite(
        original=bullet,
        tailored=candidate,
        changed=candidate.strip() != bullet.strip(),
        entities_checked=report.checked,
    )


async def tailor_bullets(
    provider: LLMProvider,
    bullets: list[str],
    job_description: str,
    corpus: SourceCorpus,
) -> TailorResult:
    """Rewrite a list of bullets. Every output is guard-checked."""
    rewrites = [
        await tailor_bullet(provider, bullet, job_description, corpus)
        for bullet in bullets
        if bullet.strip()
    ]
    return TailorResult(
        bullets=rewrites,
        rejected=sum(1 for r in rewrites if r.used_fallback),
    )
