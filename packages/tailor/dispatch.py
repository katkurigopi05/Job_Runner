"""One place that decides how a posting's résumé is tailored.

Apply (`apps/worker/apply_job.py`), the nightly batch (`batch.py`) and Compare
models (`compare.py`) each call `tailor` here, so they cannot disagree about
which document a posting gets. `TAILOR_MODE` picks:

- `rewrite`, the default: every tailorable bullet goes to the model
  (`rewrite.tailor_bullets`), as it always has.
- `targeted`, opt-in: only the bullets with a supported term to gain go
  (`targeted.revise`). A posting with nothing to gain asks no model, and no
  provider is built for it, which is why `tailor` takes a factory.

Both return one entry per bullet, and a bullet that was not sent or whose
revision was refused is the résumé's own.

A targeted document is read back from its PDF before anything attaches it
(`hold_to_pdf`). The plan is `docs/TARGETED_TAILORING_INTEGRATION.md`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from packages.core.config import get_settings
from packages.core.enums import TailorMode
from packages.core.storage import get_storage
from packages.tailor.bullets import tailorable_bullets
from packages.tailor.guard import SourceCorpus
from packages.tailor.parse import ParsedResume
from packages.tailor.publish import apply_rewrites
from packages.tailor.rewrite import BulletRewrite, TailorResult, tailor_bullets
from packages.tailor.targeted import NO_GAIN, Plan, Revision, classify, revise
from packages.tailor.verify import PdfCheck, check_pdf

if TYPE_CHECKING:
    from packages.core.models import Project, Resume
    from packages.llm.provider import LLMProvider

log = structlog.get_logger(__name__)

#: Shown wherever a score sits beside a tailored résumé. The scores count
#: matched text and read a layout. They are not a forecast.
HEURISTIC_NOTICE = (
    "Scores are heuristics about text matching and layout, not predictions of hiring success."
)


def active_mode(mode: str | None = None) -> TailorMode:
    """`mode` if the caller named one, else the setting. A typo raises."""
    return TailorMode(mode if mode is not None else get_settings().tailor_mode)


@dataclass(frozen=True)
class Tailored:
    """What one tailoring pass produced, in either mode."""

    result: TailorResult
    mode: TailorMode
    #: Targeted only. The sorting of the posting's terms, which costs no call.
    plan: Plan | None = None
    #: Targeted only, and None when nothing was asked of a model.
    revision: Revision | None = None
    #: Targeted only: the plan had no bullet to send.
    no_gain: bool = False
    gained: tuple[str, ...] = ()
    refusals: tuple[str, ...] = ()
    #: Calls actually made. Zero means no model wrote any of this document,
    #: so nothing may be recorded as having answered it.
    calls: int = 0

    @property
    def message(self) -> str:
        if self.revision is not None:
            return self.revision.message
        if self.no_gain:
            return NO_GAIN
        return f"{self.result.changed_count} bullets rewritten"


def plan_needed_calls(
    parsed: ParsedResume,
    posting_text: str,
    *,
    projects: Sequence[Project] = (),
    mode: str | None = None,
    linked: Mapping[str, str] | None = None,
) -> int:
    """The most calls tailoring this posting can make, for a quota check.

    Rewriting sends every tailorable bullet once. A targeted run sends only
    the bullets its plan names, each at most twice (`targeted.MAX_ATTEMPTS`),
    and working that out asks no model.
    """
    _, bullets = tailorable_bullets(parsed)
    if not bullets:
        return 0
    if active_mode(mode) is TailorMode.TARGETED:
        return classify(parsed, posting_text, projects=projects, linked=linked).calls_needed
    return len(bullets)


async def tailor(
    provider_factory: Callable[[], LLMProvider],
    parsed: ParsedResume,
    posting_text: str,
    *,
    projects: Sequence[Project] = (),
    corpus: SourceCorpus | None = None,
    mode: str | None = None,
    plan: Plan | None = None,
    linked: Mapping[str, str] | None = None,
) -> Tailored:
    """Tailor `parsed` for the posting, in the mode in force.

    `provider_factory` is called at most once, and not at all for a targeted
    run with nothing to gain. `linked` is `evidence.linked_evidence`: the
    repositories the owner linked to the résumé's projects.
    """
    chosen = active_mode(mode)

    if chosen is TailorMode.TARGETED:
        plan = plan or classify(parsed, posting_text, projects=projects, linked=linked)
        if not plan.targets:
            log.info("targeted_tailor_no_gain")
            unchanged = [BulletRewrite(original=b, tailored=b) for b in plan.bullets]
            return Tailored(
                result=TailorResult(bullets=unchanged), mode=chosen, plan=plan, no_gain=True
            )

        revision = await revise(
            provider_factory(), parsed, posting_text, projects=projects, plan=plan, linked=linked
        )
        return Tailored(
            result=revision.result,
            mode=chosen,
            plan=plan,
            revision=revision,
            gained=revision.gained,
            refusals=tuple(r for outcome in revision.outcomes for r in outcome.refusals),
            calls=revision.calls,
        )

    _, bullets = tailorable_bullets(parsed)
    result = await tailor_bullets(
        provider_factory(),
        bullets,
        posting_text,
        corpus or SourceCorpus.from_resume(parsed, linked=linked),
    )
    return Tailored(result=result, mode=chosen, calls=len(bullets))


@dataclass(frozen=True)
class PdfVerdict:
    """Whether a published PDF holds what its résumé says."""

    ok: bool
    #: Why not, in words for the review screen. None when it does.
    reason: str | None = None
    #: None when the file could not be read back at all.
    check: PdfCheck | None = None

    def as_dict(self) -> dict[str, Any]:
        """For the review payload. The owner's own screen, so it names what is missing."""
        check = self.check
        return {
            "ok": self.ok,
            "missing_terms": list(check.missing_terms) if check else [],
            "missing_figures": list(check.missing_figures) if check else [],
            "missing_technologies": list(check.missing_technologies) if check else [],
            "parse_findings": list(check.parse_findings) if check else [],
            "layout_findings": list(check.layout_findings) if check else [],
        }


def _what_is_missing(check: PdfCheck) -> str:
    found = []
    if check.extracted_chars == 0:
        found.append("no text could be read from it")
    if check.garbled:
        found.append("its text is garbled")
    if check.missing_terms:
        found.append(f"terms: {', '.join(check.missing_terms)}")
    if check.missing_figures:
        found.append(f"figures: {', '.join(check.missing_figures)}")
    if check.missing_technologies:
        found.append(f"technologies: {', '.join(check.missing_technologies)}")
    return "; ".join(found)


def hold_to_pdf(
    published: Resume,
    parsed: ParsedResume,
    result: TailorResult,
    posting_text: str,
    gained: Sequence[str],
) -> PdfVerdict:
    """Read the PDF that was just published and hold it to its résumé.

    A document that fails is not to be attached, and it is taken out of the
    tailoring cache here: `publish_tailored` wrote its key before anything
    had read the file, and a later run for the same posting would otherwise
    be handed the refused document as a cache hit. The row and the file stay,
    attached to nothing, for the owner to look at.

    The log line carries counts. What is missing is a term, a figure or a
    technology off the résumé (§10); the verdict has them for the owner.
    """
    try:
        pdf = get_storage().get(published.storage_ref)
        check = check_pdf(
            pdf, apply_rewrites(parsed, result, posting_text=posting_text), terms=gained
        )
    except Exception as exc:  # noqa: BLE001 - unreadable is a failed check, not a crash
        published.tailored_key = None
        log.warning("tailored_pdf_unreadable", error=type(exc).__name__)
        return PdfVerdict(ok=False, reason="the tailored PDF could not be read back")

    if check.ok:
        return PdfVerdict(ok=True, check=check)

    published.tailored_key = None
    log.warning(
        "tailored_pdf_check_failed",
        missing_terms=len(check.missing_terms),
        missing_figures=len(check.missing_figures),
        missing_technologies=len(check.missing_technologies),
        extracted_chars=check.extracted_chars,
        garbled=check.garbled,
    )
    return PdfVerdict(
        ok=False,
        reason=f"PDF verification failed ({_what_is_missing(check)})",
        check=check,
    )


__all__ = [
    "HEURISTIC_NOTICE",
    "PdfVerdict",
    "Tailored",
    "active_mode",
    "hold_to_pdf",
    "plan_needed_calls",
    "tailor",
]
