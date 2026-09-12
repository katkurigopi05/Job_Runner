"""Choose which of the owner's résumés to tailor from, per posting.

Every path read `profile.base_resume_id` and nothing else. Upload a backend
résumé, a data one and an ML one, and two of them are unreachable — the
application always starts from whichever the profile happens to point at.

That is a silent defect rather than a missing feature. Nothing fails, nothing
is logged, and the tailorer does its whole job: it rewrites the wrong document
well. The employer receives a competent ML résumé for a backend role, and the
only way to notice is to remember which résumé the profile points at.

## The choice is between *base* résumés only

`Resume` rows include tailored ones, and a résumé tailored for a different
posting is the one thing that must never be selected here — it is already
bent toward another job. `tailored_for_posting_id` is the discriminator:
`publish.py` sets it on every tailored row, including the uncacheable ones
where `tailored_key` is NULL, and `revise.py` carries it across an owner's
edit. NULL means nobody wrote this for a particular job.

## Stability beats a marginally better score

A cosine difference of a few thousandths is noise, and letting it decide would
mean the document an employer receives changes between two runs that saw the
same posting. Below `MIN_MARGIN` the profile's own `base_resume_id` wins if it
is in the running — the owner's standing choice is the tie-break, not the
float.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Profile, Resume
from packages.matching.embed import Embedder, cosine, get_embedder

#: A win narrower than this is not a win. Two résumés for adjacent roles score
#: within noise of each other on most postings, and flip-flopping between them
#: run to run would make "which document did they get" unanswerable.
MIN_MARGIN = 0.02

#: How far below the best candidate's ATS parse score a résumé may sit and
#: still be considered. Beyond this it is dropped however well it matches.
#:
#: Similarity answers "which of these is closest to the job". It cannot answer
#: "is this a document worth sending", and on the owner's own data those came
#: apart badly: an eight-line stub — `Backend engineer.`, `Senior Engineer,
#: Example Corp`, a 555 phone number — beat their real 54-line résumé 0.642 to
#: 0.609 on a support posting and would have been uploaded to the employer.
#: The margin was 0.033, outside `MIN_MARGIN`, so the tie-break correctly did
#: not fire; nothing was broken, the question was never asked. The stub parsed
#: at 56% against the real résumé's 82%.
#:
#: 0.15 is wide on purpose. This is a floor for "not fit to send", not a
#: preference for tidier formatting — two real résumés differing by a missing
#: links line should still be decided on the merits by similarity.
MAX_PARSE_DEFICIT = 0.15


@dataclass(frozen=True)
class ResumeChoice:
    """Which résumé to start from, and enough of why to render it."""

    resume_id: uuid.UUID
    version: int
    score: float
    reason: str
    #: (version, score) for every résumé considered, best first. The review
    #: screen shows this — a selection with no runners-up is unauditable.
    considered: tuple[tuple[int, float], ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "resume_id": str(self.resume_id),
            "version": self.version,
            "score": round(self.score, 4),
            "reason": self.reason,
            "considered": [
                {"version": version, "score": round(score, 4)} for version, score in self.considered
            ],
        }


def _resume_text(resume: Resume) -> str:
    lines = (resume.parsed_json or {}).get("raw_lines") or []
    return "\n".join(str(line) for line in lines)


async def base_resumes(session: AsyncSession, candidate_id: uuid.UUID) -> list[Resume]:
    """Every résumé the owner uploaded, newest first. Tailored rows excluded."""
    rows = (
        await session.scalars(
            select(Resume)
            .where(
                Resume.candidate_id == candidate_id,
                Resume.tailored_for_posting_id.is_(None),
            )
            .order_by(Resume.version.desc())
        )
    ).all()
    return [row for row in rows if _resume_text(row).strip()]


def _parse_score(resume: Resume) -> float:
    """How cleanly an ATS reads this document, ignoring any posting.

    Imported here rather than at module scope: `packages.tailor` imports from
    `packages.matching`, and taking the dependency the other way at import
    time would close the cycle.
    """
    from packages.tailor.ats import score as ats_score
    from packages.tailor.parse import ParsedResume

    try:
        parsed = ParsedResume.model_validate(resume.parsed_json or {})
    except Exception:  # noqa: BLE001 - a malformed row must not stop the pick
        return 0.0
    return ats_score(parsed, "").parse


def _readable_enough(resumes: list[Resume]) -> tuple[list[Resume], list[tuple[int, float]]]:
    """Split candidates into those fit to send and those too far behind.

    Relative to the best candidate rather than to a fixed bar. An owner whose
    résumés all parse at 60% should still get the best of them; the failure
    this prevents is a *comparatively* unreadable document winning on a
    similarity hair.

    Never returns an empty list — if every résumé is somehow below the
    threshold the best one is still returned, because refusing to pick at all
    would leave the application with no résumé, and CLAUDE.md §15 already
    records that an honest untailored résumé beats none.
    """
    if len(resumes) < 2:
        return resumes, []

    parses = {resume.id: _parse_score(resume) for resume in resumes}
    best = max(parses.values())
    floor = best - MAX_PARSE_DEFICIT

    keep = [resume for resume in resumes if parses[resume.id] >= floor]
    drop = [(resume.version, parses[resume.id]) for resume in resumes if parses[resume.id] < floor]
    return (keep or resumes), drop


async def choose_base_resume(
    session: AsyncSession,
    profile: Profile,
    posting_text: str,
    *,
    embedder: Embedder | None = None,
) -> ResumeChoice | None:
    """The résumé closest to this posting, or None when there is nothing to pick.

    Takes the posting's *text* rather than a `Posting`. The apply pipeline
    hands `_tailor` a parsed page rather than a stored row — its parameter is
    `Any` for exactly that reason — so depending on the model here would make
    this fail on the one caller that matters. Building the haystack is the
    caller's job, at the boundary where that looseness is already documented.

    None means "carry on as before": no usable base résumés, so the caller
    keeps whatever `profile.base_resume_id` already said.
    """
    resumes = await base_resumes(session, profile.candidate_id)
    if not resumes:
        return None

    if len(resumes) == 1:
        only = resumes[0]
        return ResumeChoice(
            resume_id=only.id,
            version=only.version,
            score=0.0,
            reason="the only base résumé on file",
            considered=((only.version, 0.0),),
        )

    haystack = posting_text.strip()
    if not haystack:
        return None

    # Sendability first, similarity second. A résumé an ATS reads badly scores
    # nothing with the employer however well it matches the posting, so the
    # question "is this fit to send" is settled before "which is closest".
    #
    # Deliberately scored with no posting: `ats.score` computes the keyword
    # half only when given one, and sendability is a property of the document
    # alone. Letting the posting in here would make a résumé's eligibility
    # depend on the job, which is the similarity question again.
    readable, rejected = _readable_enough(resumes)
    if len(readable) == 1 and rejected:
        only = readable[0]
        worst = ", ".join(f"v{version} at {parse:.0%}" for version, parse in rejected)
        return ResumeChoice(
            resume_id=only.id,
            version=only.version,
            score=0.0,
            reason=(
                f"the only résumé an ATS reads cleanly — dropped {worst} "
                f"against this one's {_parse_score(only):.0%}"
            ),
            considered=((only.version, 0.0),),
        )
    resumes = readable

    active = embedder or get_embedder()
    posting_vector = active.encode([haystack])[0]
    scored = sorted(
        (
            (cosine(posting_vector, active.encode([_resume_text(resume)])[0]), resume)
            for resume in resumes
        ),
        key=lambda pair: pair[0],
        reverse=True,
    )

    best_score, best = scored[0]
    runner_up = scored[1][0]
    considered = tuple((resume.version, round(score, 6)) for score, resume in scored)

    margin = best_score - runner_up
    if margin < MIN_MARGIN and profile.base_resume_id is not None:
        standing = next((r for _, r in scored if r.id == profile.base_resume_id), None)
        if standing is not None:
            standing_score = next(s for s, r in scored if r.id == standing.id)
            return ResumeChoice(
                resume_id=standing.id,
                version=standing.version,
                score=standing_score,
                reason=(
                    f"kept the profile's own résumé — the best match won by only "
                    f"{margin:.3f}, which is inside the noise"
                ),
                considered=considered,
            )

    return ResumeChoice(
        resume_id=best.id,
        version=best.version,
        score=best_score,
        reason=(
            f"closest of {len(resumes)} résumés to this posting "
            f"({best_score:.3f}, next {runner_up:.3f})"
        ),
        considered=considered,
    )
