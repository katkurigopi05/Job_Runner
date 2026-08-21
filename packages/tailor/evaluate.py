"""Is the tailoring any *good*? Nothing here asks whether it is safe.

The fabrication guard answers one question well: does this output claim
something the résumé does not support. It cannot answer the other one. A
rewrite that returns the source line verbatim passes the guard perfectly and
did nothing; so does a rewrite that strips every specific and leaves a bland
sentence. Both are indistinguishable from success in every test this project
has.

That gap is what `docs/PARITY.md` calls "golden-set evals" and what CLAUDE.md
§15 is complaining about when it says the gate fixtures "do not answer the
question the gate was written to ask".

## The three failures this measures

**Silent no-op.** The model returned the source line, the guard accepted it,
and `changed` was never checked. The application ships an untailored résumé
while the review screen reports a successful tailoring pass.

**Silent fallback.** The guard refused every rewrite, each bullet fell back to
its original, and the result is byte-identical to a no-op. The rejection
reasons exist in the report — nothing aggregated them into a number anyone
would notice moving.

**Keyword uptake.** §2.1 permits injecting posting vocabulary *the source
already supports*, and that is the entire point of tailoring for an ATS. No
test has ever checked that a single supported term made it into the output.

## What these numbers cannot tell apart

A high rejection rate means one of two things and this cannot say which. The
first run against real postings refused **100%** of rewrites for "Product
Designer, Growth" — with a backend résumé as the source. That is the guard
working exactly as §2.1 requires: there is nothing in a backend résumé that
supports a product-design claim, so every attempt was correctly refused.

So a posting the owner would never apply to scores as badly as a broken
tailorer. Read the rate *across* the set, not per posting, and read it against
postings the owner actually wants — which is what the `/swipe` decisions will
eventually provide.

## Why the numbers are deterministic

An LLM-as-judge is the obvious design and it is a bad regression gate: the
judge drifts, so a score that moves tells you nothing about which side moved.
Everything here is computed from the text. A judge can be layered on top for
prose quality, but the gate is arithmetic.

The provider is whatever the caller passes, so this runs against
`StubProvider` in tests and a real model when the owner wants real numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from packages.llm.provider import LLMProvider
from packages.tailor.guard import SourceCorpus
from packages.tailor.keywords import analyze
from packages.tailor.parse import ParsedResume
from packages.tailor.rewrite import TailorResult, tailor_bullets

#: Below this share of bullets actually changing, tailoring is doing nothing
#: worth the call. Not a quality bar — a liveness one.
MIN_CHANGE_RATE = 0.20

#: Above this share of rewrites refused, the model is reaching for unsupported
#: material so often that the prompt or the model is wrong.
MAX_REJECTION_RATE = 0.60


@dataclass(frozen=True)
class Quality:
    """What one tailoring run actually did."""

    bullets: int = 0
    changed: int = 0
    rejected: int = 0
    #: Accepted, and identical to the source. The silent no-op.
    unchanged_accepted: int = 0
    #: Rewrites the *provider* never returned — a timeout, a 404, an
    #: exhausted quota. `tailor_bullet` turns those into the same fallback
    #: a guard refusal produces, so without counting them separately a dead
    #: API reads as a model with poor judgement. That is not hypothetical:
    #: a Gemini run scored 100% refused on every posting, and the cause was
    #: a 404 on every call.
    provider_errors: int = 0
    #: Supported posting terms present in the output but not in the source.
    terms_taken_up: int = 0
    #: Supported posting terms that were available to take up.
    terms_available: int = 0
    #: Output words divided by source words. Far below 1.0 means the model is
    #: dropping detail rather than re-emphasising it.
    length_ratio: float = 1.0

    @property
    def change_rate(self) -> float:
        return self.changed / self.bullets if self.bullets else 0.0

    @property
    def rejection_rate(self) -> float:
        return self.rejected / self.bullets if self.bullets else 0.0

    @property
    def uptake_rate(self) -> float:
        """Share of available supported terms that reached the output.

        The number tailoring exists to move. Zero means the rewrite is prose
        editing with no ATS effect at all.
        """
        return self.terms_taken_up / self.terms_available if self.terms_available else 0.0

    @property
    def problems(self) -> list[str]:
        """Named failures, empty when the run was healthy."""
        found: list[str] = []
        if self.provider_errors:
            # Reported first and alone: nothing else measured here means
            # anything when the model never answered.
            return [
                f"{self.provider_errors}/{self.bullets} calls never reached the model — "
                "this measures the provider, not the tailoring"
            ]
        if self.bullets and self.change_rate < MIN_CHANGE_RATE:
            found.append(
                f"only {self.change_rate:.0%} of bullets changed — tailoring is close to a no-op"
            )
        if self.rejection_rate > MAX_REJECTION_RATE:
            found.append(
                f"{self.rejection_rate:.0%} of rewrites refused — the model keeps reaching "
                "for unsupported material"
            )
        if self.terms_available and self.uptake_rate == 0.0:
            found.append(
                f"none of the {self.terms_available} supported posting terms reached the output"
            )
        if self.length_ratio < 0.7:
            found.append(
                f"output is {self.length_ratio:.0%} the length of the source — detail is being "
                "dropped, not re-emphasised"
            )
        return found

    @property
    def healthy(self) -> bool:
        return not self.problems


@dataclass
class GoldenReport:
    """One evaluation run across the whole golden set."""

    per_posting: list[tuple[str, Quality]] = field(default_factory=list)

    @property
    def bullets(self) -> int:
        return sum(q.bullets for _, q in self.per_posting)

    @property
    def changed(self) -> int:
        return sum(q.changed for _, q in self.per_posting)

    @property
    def rejected(self) -> int:
        return sum(q.rejected for _, q in self.per_posting)

    @property
    def change_rate(self) -> float:
        return self.changed / self.bullets if self.bullets else 0.0

    @property
    def rejection_rate(self) -> float:
        return self.rejected / self.bullets if self.bullets else 0.0

    @property
    def uptake_rate(self) -> float:
        taken = sum(q.terms_taken_up for _, q in self.per_posting)
        available = sum(q.terms_available for _, q in self.per_posting)
        return taken / available if available else 0.0

    @property
    def provider_errors(self) -> int:
        return sum(q.provider_errors for _, q in self.per_posting)

    @property
    def unhealthy(self) -> list[tuple[str, list[str]]]:
        return [(name, q.problems) for name, q in self.per_posting if q.problems]

    def summary(self) -> str:
        if self.provider_errors:
            return (
                f"{self.provider_errors}/{self.bullets} calls failed at the provider — "
                "no tailoring was measured"
            )
        return (
            f"{len(self.per_posting)} postings, {self.bullets} bullets: "
            f"{self.change_rate:.0%} changed, {self.rejection_rate:.0%} refused, "
            f"{self.uptake_rate:.0%} term uptake"
        )


def _words(text: str) -> int:
    return len(text.split())


def measure(result: TailorResult, job_description: str, corpus: SourceCorpus) -> Quality:
    """Score one tailoring run. No model involved — this is arithmetic."""
    if not result.bullets:
        return Quality()

    terms = analyze(job_description, corpus)
    supported = {term.lower() for term in terms.supported}

    source_words = sum(_words(b.original) for b in result.bullets)
    output_words = sum(_words(b.tailored) for b in result.bullets)

    taken_up: set[str] = set()
    for bullet in result.bullets:
        before = bullet.original.lower()
        after = bullet.tailored.lower()
        for term in supported:
            # Present now, absent before: the rewrite worked it in. A term
            # already in the source is not uptake, it is coincidence.
            if term in after and term not in before:
                taken_up.add(term)

    return Quality(
        bullets=len(result.bullets),
        changed=sum(1 for b in result.bullets if b.changed),
        rejected=sum(1 for b in result.bullets if b.used_fallback),
        provider_errors=sum(
            1
            for b in result.bullets
            if b.rejected_reason and b.rejected_reason.startswith("provider error")
        ),
        unchanged_accepted=sum(1 for b in result.bullets if not b.used_fallback and not b.changed),
        terms_taken_up=len(taken_up),
        terms_available=len(supported),
        length_ratio=(output_words / source_words) if source_words else 1.0,
    )


async def evaluate_golden(
    provider: LLMProvider,
    resume: ParsedResume,
    postings: list[dict[str, str]],
    *,
    section: str = "experience",
) -> GoldenReport:
    """Tailor one résumé against every posting in the set and measure each.

    The postings are real ones, crawled from live boards — not written to suit
    the assertion. That is the whole distinction §15 draws, and it is the
    reason this can catch a regression the hand-written fixtures cannot.
    """
    bullets = [line for line in resume.section(section) if line.strip()]
    corpus = SourceCorpus.from_resume(resume)
    report = GoldenReport()

    for posting in postings:
        description = posting.get("description") or ""
        if not description.strip() or not bullets:
            continue
        result = await tailor_bullets(provider, bullets, description, corpus)
        label = f"{posting.get('company') or '?'} — {posting.get('title') or '?'}"
        report.per_posting.append((label, measure(result, description, corpus)))

    return report
