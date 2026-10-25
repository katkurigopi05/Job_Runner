"""Tailor the same posting with two models and let the owner choose.

§7 made the provider settable per task, so the owner can run tailoring locally
or in the cloud. What it could not answer is the question that actually decides
the setting: *is the cloud one better for my résumé and this job?* The evidence
for that lives in the two documents side by side, and until now producing them
meant editing `.env`, re-running, and holding the first result in your head.

## Both sides go through the guard

`tailor_bullets` runs the §2.1 fabrication check on every rewrite and falls back
to the original line when it refuses, so both candidates here are vetted by
construction. That is not incidental to the feature — a comparison screen offers
each side as a thing the owner may choose and send, and an unvetted draft
presented as a choosable option is a fabricated bullet with a button under it.
The refusal counts are reported per side, because "the local model was refused
nine times" is itself one of the more useful things a comparison can tell you.

## On demand, and cached

Each side is a separate remote upload of the owner's résumé under §2.8, so this
runs when the owner asks for it rather than on every application. The tailoring
cache is consulted first and keyed per provider, so comparing twice, or
comparing a posting one side has already been tailored for, sends nothing. The
cheapest upload is still the one not made.

A side that cannot run — no cloud provider configured, the daily allowance
spent, Ollama not started — is reported as a failed candidate with the reason,
not dropped. A comparison silently missing its second half looks like a verdict.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.enums import TailorMode
from packages.core.models import Posting, Profile, Project, Resume
from packages.github.select import relevant_for_posting
from packages.llm.provider import build_provider
from packages.llm.router import (
    LOCAL_PROVIDER,
    cloud_for_tailoring,
    comparable_clouds,
    is_comparable_cloud,
    tailor_resume,
)
from packages.matching.embed import get_embedder
from packages.tailor.bullets import tailorable_bullets
from packages.tailor.cache import find_cached, tailoring_key
from packages.tailor.diff import summarize
from packages.tailor.dispatch import HEURISTIC_NOTICE, hold_to_pdf, tailor
from packages.tailor.evidence import evidence_digest, linked_evidence
from packages.tailor.guard import SourceCorpus
from packages.tailor.parse import ParsedResume
from packages.tailor.publish import publish_tailored
from packages.tailor.rewrite import tailor_bullets
from packages.tailor.targeted import NO_GAIN, classify

log = structlog.get_logger(__name__)


class CannotCompare(RuntimeError):
    """There is nothing to compare — not one side failing, but no comparison.

    Separate from a failed `Candidate` on purpose. A side that could not run is
    a result the owner should see beside the side that did; a missing résumé or
    an empty posting means there was never anything to show, and reporting that
    as two failed columns would dress up a precondition as a model outcome.
    """


@dataclass(frozen=True)
class Candidate:
    """One model's attempt at the same posting.

    `requested` and `answered_by` are separate on purpose. Asking for Gemini and
    being answered by llama3.1 is what §7's fallback does when the allowance is
    spent, and a comparison that labelled that side "gemini" would be comparing
    the local model against itself while telling the owner otherwise.
    """

    requested: str
    answered_by: str | None = None
    resume_id: uuid.UUID | None = None
    changed: int = 0
    unchanged: int = 0
    #: Rewrites the fabrication guard refused — what the model tried to write.
    rejected: int = 0
    #: Bullets the model never answered — the network, not the model's judgment.
    #: Reported apart from `rejected` because a provider that was down would
    #: otherwise be shown as one that kept trying to invent, which is the
    #: opposite reading and the one a comparison must not invite.
    provider_failures: int = 0
    unified: str = ""
    changes: list[dict[str, Any]] = field(default_factory=list)
    reused: bool = False
    #: Why this side has nothing to show. None when it ran.
    error: str | None = None
    #: How this side was tailored. `targeted` is the column that revises only
    #: the bullets with a supported term to gain, and the four fields below
    #: are that column's.
    mode: str = TailorMode.REWRITE.value
    gained: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    #: What reading the published PDF back found. None when nothing was read.
    pdf_check: dict[str, Any] | None = None
    heuristic_notice: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "answered_by": self.answered_by,
            "resume_id": str(self.resume_id) if self.resume_id else None,
            "changed": self.changed,
            "unchanged": self.unchanged,
            "rejected": self.rejected,
            "provider_failures": self.provider_failures,
            "unified": self.unified,
            "changes": self.changes,
            "reused": self.reused,
            "error": self.error,
            "mode": self.mode,
            "gained": self.gained,
            "refusals": self.refusals,
            "pdf_check": self.pdf_check,
            "heuristic_notice": self.heuristic_notice,
        }


async def tailor_with(
    session: AsyncSession,
    *,
    provider_name: str,
    resume: Resume,
    parsed: ParsedResume,
    bullets: list[str],
    posting_text: str,
    projects: list[Project],
    content_hash: str | None,
    posting_id: uuid.UUID | None,
    mode: str = TailorMode.REWRITE.value,
) -> Candidate:
    """One side of the comparison, guard-checked and stored.

    Every failure becomes a `Candidate` carrying the reason rather than an
    exception, because one side being unavailable is a normal outcome here —
    the allowance runs out, Ollama is not running, no cloud key is set — and it
    must not take the other side down with it.
    """
    if TailorMode(mode) is TailorMode.TARGETED:
        return await _targeted_side(
            session,
            resume=resume,
            parsed=parsed,
            bullets=bullets,
            posting_text=posting_text,
            projects=projects,
            content_hash=content_hash,
            posting_id=posting_id,
        )

    try:
        provider = build_provider(provider_name)
    except Exception as exc:  # noqa: BLE001 - an unconfigured side is a result, not a crash
        return Candidate(requested=provider_name, error=_reason(exc))

    linked = await linked_evidence(session, resume.candidate_id)
    cache_key = tailoring_key(
        source_resume_id=resume.id,
        content_hash=content_hash,
        projects=projects,
        provider=getattr(provider, "name", provider_name),
        model=getattr(provider, "model", None),
        evidence=evidence_digest(linked),
    )
    cached = await find_cached(session, candidate_id=resume.candidate_id, key=cache_key)
    if cached is not None:
        # Already tailored for exactly this, by exactly this model. Re-running
        # would upload the résumé again to produce a document we are holding.
        return Candidate(
            requested=provider_name,
            answered_by=cached.tailored_by,
            resume_id=cached.id,
            reused=True,
        )

    try:
        result = await tailor_bullets(
            provider, bullets, posting_text, SourceCorpus.from_resume(parsed, linked=linked)
        )
    except Exception as exc:  # noqa: BLE001 - see the docstring
        log.warning("compare_side_failed", provider=provider_name, error=type(exc).__name__)
        return Candidate(requested=provider_name, error=_reason(exc))

    # After the call, not before — §7's fallback rewrites this only once the
    # primary has actually failed.
    answered_by = getattr(provider, "answered_by", None) or getattr(provider, "name", provider_name)
    summary = summarize(result)

    published = await publish_tailored(
        session,
        candidate_id=resume.candidate_id,
        parsed=parsed,
        result=result,
        projects=projects,
        posting_text=posting_text,
        tailored_key=cache_key,
        posting_id=posting_id,
        answered_by=answered_by,
    )

    return Candidate(
        requested=provider_name,
        answered_by=answered_by,
        resume_id=published.id if published is not None else None,
        changed=summary.changed,
        unchanged=summary.unchanged,
        rejected=summary.rejected,
        provider_failures=summary.provider_failures,
        unified=summary.unified,
        changes=[change.model_dump() for change in summary.changes],
        # A render failure is not a tailoring failure, but it does mean there is
        # no document to choose, so it has to be visible on this side.
        error=None if published is not None else "the tailored PDF could not be rendered",
    )


def _reason(exc: Exception) -> str:
    """A message the owner can act on, without leaking a key or a payload.

    `QuotaExceeded` already phrases itself for a human and names the remedy, so
    it is passed through. Anything else is reported by type: §10 forbids logging
    résumé contents, and an arbitrary provider exception can carry the prompt.
    """
    from packages.llm.quota import QuotaExceeded

    if isinstance(exc, QuotaExceeded):
        return str(exc)
    return f"{type(exc).__name__} — check that the provider is configured and reachable"


def _requested_clouds(cloud: str | None, clouds: Sequence[str] | None) -> tuple[str, ...]:
    """The remote sides the owner asked for, de-duplicated and in order.

    Two fields because they answer different questions and both are worth
    asking. `cloud` is "would my cloud provider have done better than local",
    which is one upload and the common case. `clouds` is "which of these
    should I be using", which is one upload *per provider named* — a §2.8
    multiplier, and the reason it has to be spelled out rather than defaulted
    to everything configured.

    `clouds` wins when both are given: it is the more specific request, and
    silently running a fourth column the caller did not list would be exactly
    the surprise upload this guards against.

    Order is preserved and duplicates dropped, so naming a provider twice does
    not send the résumé twice.
    """
    named = list(clouds) if clouds else ([cloud] if cloud else [])
    seen: set[str] = set()
    ordered: list[str] = []
    for name in named:
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)
    return tuple(ordered)


async def _targeted_side(
    session: AsyncSession,
    *,
    resume: Resume,
    parsed: ParsedResume,
    bullets: list[str],
    posting_text: str,
    projects: list[Project],
    content_hash: str | None,
    posting_id: uuid.UUID | None,
) -> Candidate:
    """The targeted revision as a column: only the bullets with a term to gain.

    Asked for by name (`targeted` on the request) and sent to the provider
    real tailoring uses, so the column is the document `TAILOR_MODE=targeted`
    would attach. Three things it must not do:

    - **Build a provider with nothing to send.** The posting's terms are
      sorted first, which asks no model. No bullet to revise and no project
      to add is a column that says so.
    - **Name a model that did not answer.** One provider is built and it is
      the one read afterwards. Building one to learn its name and a second to
      make the calls would report the first, and with `LLM_FALLBACK_LOCAL` on
      and the allowance spent that is a local model's bullet under a cloud
      model's name.
    - **Offer a document that failed its PDF.** It has no résumé id, so it
      cannot be chosen, and `hold_to_pdf` takes it out of the cache so the
      next comparison does not offer it as "reused".
    """
    label = TailorMode.TARGETED.value

    def side(**fields: Any) -> Candidate:
        return Candidate(requested=label, mode=label, heuristic_notice=HEURISTIC_NOTICE, **fields)

    try:
        linked = await linked_evidence(session, resume.candidate_id)
        plan = classify(parsed, posting_text, projects=projects, linked=linked)
        if not plan.targets and not projects:
            return side(unchanged=len(bullets), error=f"{NO_GAIN}; nothing was sent")

        provider = tailor_resume()
        name = getattr(provider, "name", label)
        cache_key = tailoring_key(
            source_resume_id=resume.id,
            content_hash=content_hash,
            projects=projects,
            provider=name,
            model=getattr(provider, "model", None),
            mode=TailorMode.TARGETED,
            evidence=evidence_digest(linked),
        )
        cached = await find_cached(session, candidate_id=resume.candidate_id, key=cache_key)
        if cached is not None:
            return side(answered_by=cached.tailored_by, resume_id=cached.id, reused=True)

        tailored = await tailor(
            lambda: provider,
            parsed,
            posting_text,
            projects=projects,
            mode=TailorMode.TARGETED,
            plan=plan,
            linked=linked,
        )
    except Exception as exc:  # noqa: BLE001 - an unconfigured side is a result, not a crash
        log.warning("compare_side_failed", provider=label, error=type(exc).__name__)
        return side(error=_reason(exc))

    # After the calls, as on the other sides. And nobody when the plan had no
    # bullet to send: that document is the base with its projects added.
    answered_by = (getattr(provider, "answered_by", None) or name) if tailored.calls else None
    summary = summarize(tailored.result)

    published = await publish_tailored(
        session,
        candidate_id=resume.candidate_id,
        parsed=parsed,
        result=tailored.result,
        projects=projects,
        posting_text=posting_text,
        tailored_key=cache_key,
        posting_id=posting_id,
        answered_by=answered_by,
        mode=TailorMode.TARGETED,
    )

    error = None
    verdict = None
    if published is None:
        error = "the tailored PDF could not be rendered"
    else:
        verdict = hold_to_pdf(published, parsed, tailored.result, posting_text, tailored.gained)
        if not verdict.ok:
            error = f"{verdict.reason}; not offered"

    return side(
        answered_by=answered_by,
        resume_id=published.id if published is not None and error is None else None,
        changed=summary.changed,
        unchanged=summary.unchanged,
        rejected=summary.rejected,
        provider_failures=summary.provider_failures,
        unified=summary.unified,
        changes=[change.model_dump() for change in summary.changes],
        gained=list(tailored.gained),
        refusals=list(tailored.refusals),
        pdf_check=verdict.as_dict() if verdict is not None else None,
        error=error,
    )


async def compare_tailorings(
    session: AsyncSession,
    *,
    profile: Profile,
    posting: Posting,
    cloud: str | None = None,
    clouds: Sequence[str] | None = None,
    targeted: bool = False,
) -> list[Candidate]:
    """Tailor this posting with the local model and with a cloud one.

    Order is local first. The local side costs nothing and cannot fail on
    quota, so when the remote half is refused the owner still has a document
    and a reason rather than an empty screen.

    `cloud` names the remote half for *this comparison only*. Omitted, it is
    whatever real tailoring would use (`cloud_for_tailoring`), which is the
    right default: the usual question is "would my configured cloud provider
    have done better than local".

    It is settable because that default could not answer the question for
    OpenRouter. §7 keeps `openrouter` out of `QUALITY_ORDER` on purpose, so the
    only way to make it the cloud column was `LLM_TASK_TAILOR=openrouter` —
    which also redirects every real tailoring call to it. The owner had to
    *adopt* a provider in order to evaluate it, which is the exact friction this
    comparison exists to remove, and it pointed the wrong way: the provider
    hardest to name is the one whose output most deserves a look first.

    Naming it here changes nothing outside this call. No setting moves, real
    tailoring is untouched, and the next application routes exactly as before —
    the same shape as §14's per-question provider choice in `/chat`.

    Raises only for the cases where there is nothing to compare at all — no base
    résumé, a posting with no text, or a named cloud that cannot answer.
    Everything else that can go wrong belongs to one side and is reported there.
    """
    requested = _requested_clouds(cloud, clouds)
    for name in requested:
        if not is_comparable_cloud(name):
            # A precondition, not a model outcome, so it raises rather than
            # becoming a failed column — the owner asked for a specific
            # comparison and did not get it, and a column reading "openrouter:
            # unavailable" beside a local one would look like a verdict on
            # OpenRouter.
            raise CannotCompare(
                f"{name!r} cannot be a remote half of a comparison. Choose from "
                f"{comparable_clouds() or ['— none configured —']}, or name none "
                "to use whatever real tailoring would."
            )
    if profile.base_resume_id is None:
        raise CannotCompare("this profile has no base résumé to tailor")

    posting_text = posting.description_raw or ""
    if not posting_text.strip():
        raise CannotCompare("this posting has no description to tailor against")

    resume = await session.get(Resume, profile.base_resume_id)
    if resume is None or not resume.parsed_json:
        raise CannotCompare("the base résumé has not been parsed")

    parsed = ParsedResume.model_validate(resume.parsed_json)
    section, bullets = tailorable_bullets(parsed)
    if not bullets:
        raise CannotCompare("the base résumé has no experience or project bullets to rewrite")
    log.info("compare_section_chosen", section=section)

    # The same policy the apply pipeline uses (`apply_job._projects_for`), so
    # the documents being compared are the ones an application would actually
    # send. A different project set here would compare two things neither of
    # which is what gets uploaded.
    inventory = list(
        (
            await session.scalars(
                select(Project).where(Project.candidate_id == resume.candidate_id)
            )
        ).all()
    )
    projects = relevant_for_posting(inventory, posting_text, embedder=get_embedder())

    if requested:
        remotes = list(requested)
    else:
        fallback = cloud_for_tailoring()
        remotes = [fallback] if fallback else []
    sides = [LOCAL_PROVIDER, *remotes]

    candidates: list[Candidate] = []
    for name in sides:
        candidates.append(
            await tailor_with(
                session,
                provider_name=name,
                resume=resume,
                parsed=parsed,
                bullets=bullets,
                posting_text=posting_text,
                projects=projects,
                content_hash=posting.content_hash,
                posting_id=posting.id,
            )
        )

    if not remotes:
        candidates.append(
            Candidate(
                requested="cloud",
                error=(
                    "no remote provider is configured, so there is nothing to compare "
                    "against. Set a key and name it with LLM_TASK_TAILOR, or keep "
                    "tailoring local."
                ),
            )
        )

    # Last, and only when asked for: it is one more upload of the bullets it
    # sends, to the provider real tailoring uses.
    if targeted:
        candidates.append(
            await tailor_with(
                session,
                provider_name=TailorMode.TARGETED.value,
                resume=resume,
                parsed=parsed,
                bullets=bullets,
                posting_text=posting_text,
                projects=projects,
                content_hash=posting.content_hash,
                posting_id=posting.id,
                mode=TailorMode.TARGETED.value,
            )
        )

    return candidates
