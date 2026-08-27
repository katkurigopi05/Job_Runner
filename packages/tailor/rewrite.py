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
from packages.tailor.keywords import TermReport, analyze, borrowed_terms
from packages.tailor.recombination import find as find_recombinations

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
    #: The model never answered — a transport error, an empty completion, a
    #: spent allowance.
    #:
    #: Separate from the guard refusing an answer, and the distinction is the
    #: whole point of reporting refusals at all. "The guard refused this" is a
    #: statement about what the model tried to write; "the provider failed" is a
    #: statement about the network. Counted together, a model that never spoke
    #: is indistinguishable from one that kept trying to invent — which is
    #: exactly backwards, and on a comparison screen it reads as a verdict on
    #: the wrong thing.
    provider_failed: bool = False

    @property
    def used_fallback(self) -> bool:
        """The original line was kept, for either reason."""
        return self.rejected_reason is not None

    @property
    def guard_refused(self) -> bool:
        """The model answered and the fabrication guard rejected the answer."""
        return self.rejected_reason is not None and not self.provider_failed


class TailorResult(BaseModel):
    bullets: list[BulletRewrite] = Field(default_factory=list)
    #: Rewrites the fabrication guard refused. Provider failures are *not* here.
    rejected: int = 0
    #: Bullets where the model never answered. See `BulletRewrite.provider_failed`.
    provider_failures: int = 0

    @property
    def tailored_lines(self) -> list[str]:
        return [b.tailored for b in self.bullets]

    @property
    def changed_count(self) -> int:
        return sum(1 for b in self.bullets if b.changed)


def _user_prompt(bullet: str, job_description: str, terms: TermReport | None = None) -> str:
    """The posting, plus which of its vocabulary the résumé actually backs.

    Naming the supported terms is what makes §2.1's "inject keywords already
    supported by the source" a computed set rather than a hope. Naming the
    unsupported ones matters too: they are precisely what a model reaches for
    when it invents, and a model told what not to say does it less often.
    """
    parts = [f"Job description:\n{job_description.strip()[:4000]}"]

    if terms is not None:
        if terms.supported:
            parts.append("SUPPORTED TERMS (safe to work in):\n" + ", ".join(terms.supported))
        if terms.missing:
            parts.append("OFF-LIMITS TERMS (never use):\n" + ", ".join(terms.missing[:25]))

    parts.append(f"Original bullet:\n{bullet.strip()}")
    parts.append("Rewritten bullet:")
    return "\n\n".join(parts)


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
    original: str,
    candidate: str,
    corpus: SourceCorpus,
    forbidden: tuple[str, ...] = (),
) -> tuple[bool, str | None, GuardReport]:
    """Decide whether a rewrite may be used.

    Rejection reasons, in order of how often they matter: a term the posting
    asked for that the résumé does not support, fabricated content, a bullet
    replaced wholesale rather than rewritten, implausible growth, or an empty
    answer.

    `forbidden` is the posting's vocabulary the résumé does not back, computed
    by `packages/tailor/keywords.py`. It is checked *here* rather than only
    named in the prompt, because the prompt already named it and every model
    tested used the terms anyway:

        Built backend services in Python.
          -> Designed and developed high-throughput payment services in Python.

    "payment" is an ordinary lowercase word, so the entity check traces it to
    nothing and flags nothing — the résumé would claim payments experience its
    owner does not have. Prompts are advisory; this is the guarantee.

    The fabrication check is scoped to the entry the original bullet came
    from. Checking against the whole résumé would accept a rewrite that moved
    one employer's metric onto another employer's bullet — every fact true
    somewhere, the sentence false where it stands.
    """
    report = check(candidate, corpus, scope=corpus.locate(original))

    if not candidate:
        return False, "model returned nothing", report

    borrowed = borrowed_terms(original, candidate, forbidden)
    if borrowed:
        return (
            False,
            "takes " + ", ".join(repr(term) for term in borrowed) + " from the posting; "
            "the résumé does not support it",
            report,
        )
    if not report.ok:
        return False, report.summary(), report

    # `check` verifies tokens, and a token-level check has no representation of
    # which words stood *next to* which — so a claim assembled entirely from
    # supported vocabulary passes it. "Kubernetes cluster administration" is
    # clean when the résumé has Kubernetes under one employer and cluster
    # administration under another, and the sentence asserts something it never
    # did. Gate 3 asks that every noun-phrase entity trace to the source;
    # without this, what runs answers a weaker question.
    #
    # packages/tailor/recombination.py was written for exactly this and nothing
    # called it. Measured before wiring, per §7: across 54 bullets tailored by
    # llama3.1 over Gate 3's own fixtures, 29 passed the token check and this
    # refused **none** of them. It is insurance with no observed cost, not a
    # tightening that trades acceptance for safety.
    #
    # Same scope as the check above, for the same reason: the question is
    # whether these words stood together *in this entry*.
    recombined = find_recombinations(candidate, corpus, scope=corpus.locate(original))
    if recombined:
        joined = "; ".join(
            f"{r.first.normalized!r} and {r.second.normalized!r}" for r in recombined[:3]
        )
        return (
            False,
            f"combines {joined} — each appears in the source, never together",
            report,
        )

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
    terms: TermReport | None = None,
) -> BulletRewrite:
    """Rewrite one bullet, falling back to the original if it does not vet."""
    try:
        raw = await provider.complete(
            SYSTEM_PROMPT,
            # `terms` is the supported/off-limits split from keywords.py —
            # main's work. `temperature` is the per-task setting — mobile's.
            # Both belong: one shapes what the model may say, the other how
            # much latitude it takes while saying it.
            _user_prompt(bullet, job_description, terms),
            max_tokens=300,
            temperature=temperature_for("tailor_resume"),
        )
    except Exception as exc:  # noqa: BLE001 - a provider failure is a fallback
        log.warning("tailor_provider_failed", error=type(exc).__name__)
        return BulletRewrite(
            original=bullet,
            tailored=bullet,
            rejected_reason=f"provider error: {type(exc).__name__}",
            provider_failed=True,
        )

    candidate = _clean(raw)
    forbidden = tuple(terms.missing) if terms else ()
    accepted, reason, report = vet(bullet, candidate, corpus, forbidden)

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
    # Computed once for the whole posting rather than per bullet: the split is
    # a property of the résumé and the job, not of any one line.
    terms = analyze(job_description, corpus)
    rewrites = [
        await tailor_bullet(provider, bullet, job_description, corpus, terms)
        for bullet in bullets
        if bullet.strip()
    ]
    return TailorResult(
        bullets=rewrites,
        rejected=sum(1 for r in rewrites if r.guard_refused),
        provider_failures=sum(1 for r in rewrites if r.provider_failed),
    )
