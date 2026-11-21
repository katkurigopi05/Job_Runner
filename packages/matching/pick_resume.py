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

## Requirements backed first, similarity second (2026-11-21)

Similarity reads prose, and a posting states its stack in a line or two. The
résumé that backs more of the posting's *direct* requirements (required, not
one choice among several, quoted: `skill_links.direct_requirements`, the
graph's own reading) is the one an ATS and a recruiter will match, so a whole
requirement more decides before any cosine is read. Between résumés that back
the same number, similarity decides as above; with no direct requirement read,
it decides alone. Résumé skills are `gaps.resume_skills`, what
`skill_links.link_resume` saves.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Profile, Resume
from packages.matching.embed import Embedder, cosine, get_embedder
from packages.matching.gaps import resume_skills, skill_label
from packages.matching.skill_links import direct_requirements

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
class Considered:
    version: int
    score: float
    #: How many of the posting's direct requirements this résumé backs; None
    #: when the posting states none.
    covered: int | None = None


@dataclass(frozen=True)
class ResumeChoice:
    """Which résumé to start from, and enough of why to render it."""

    resume_id: uuid.UUID
    version: int
    score: float
    reason: str
    #: Every résumé considered, best first. The review screen shows this — a
    #: selection with no runners-up is unauditable.
    considered: tuple[Considered, ...] = ()
    #: The posting's direct requirements, and those the chosen résumé does not back.
    required: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "resume_id": str(self.resume_id),
            "version": self.version,
            "score": round(self.score, 4),
            "reason": self.reason,
            "considered": [
                {"version": c.version, "score": round(c.score, 4)}
                | ({"covered": c.covered} if c.covered is not None else {})
                for c in self.considered
            ],
            "required": [skill_label(key) for key in self.required],
            "missing": [skill_label(key) for key in self.missing],
        }


def _resume_text(resume: Resume) -> str:
    lines = (resume.parsed_json or {}).get("raw_lines") or []
    return "\n".join(str(line) for line in lines)


def _choice(
    chosen: Resume,
    score: float,
    reason: str,
    ranked: Sequence[tuple[float, Resume]],
    required: frozenset[str],
) -> ResumeChoice:
    """The choice, with each résumé's coverage of `required` beside its similarity."""
    skills = {resume.id: resume_skills(_resume_text(resume)) for _, resume in ranked}
    skills.setdefault(chosen.id, resume_skills(_resume_text(chosen)))
    return ResumeChoice(
        resume_id=chosen.id,
        version=chosen.version,
        score=score,
        reason=reason,
        considered=tuple(
            Considered(
                resume.version,
                round(similarity, 6),
                len(required & skills[resume.id]) if required else None,
            )
            for similarity, resume in ranked
        ),
        required=tuple(sorted(required, key=skill_label)),
        missing=tuple(sorted(required - skills[chosen.id], key=skill_label)),
    )


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
    required = direct_requirements(posting_text)

    if len(resumes) == 1:
        only = resumes[0]
        return _choice(only, 0.0, "the only base résumé on file", [(0.0, only)], required)

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
        reason = (
            f"the only résumé an ATS reads cleanly — dropped {worst} "
            f"against this one's {_parse_score(only):.0%}"
        )
        return _choice(only, 0.0, reason, [(0.0, only)], required)
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

    return _decide(scored, profile, required)


def _decide(
    scored: list[tuple[float, Resume]], profile: Profile, required: frozenset[str]
) -> ResumeChoice:
    """More direct requirements backed first; similarity among those that tie.

    `scored` is every readable résumé, most similar first, at least two.
    """
    covered = {
        resume.id: len(required & resume_skills(_resume_text(resume))) for _, resume in scored
    }
    # Stable: within one coverage, the most similar stays first.
    ranked = sorted(scored, key=lambda pair: -covered[pair[1].id])
    best_cover = covered[ranked[0][1].id]
    leaders = [pair for pair in ranked if covered[pair[1].id] == best_cover]
    of = f"{best_cover} of {len(required)} required skills"
    if len(leaders) == 1:
        score, best = leaders[0]
        next_cover = covered[ranked[1][1].id]
        reason = f"backs {of}, more than any other résumé (next {next_cover})"
        return _choice(best, score, reason, ranked, required)

    best_score, best = leaders[0]
    runner_up = leaders[1][0]
    margin = best_score - runner_up
    backing = f" that back {of}" if best_cover else ""
    unbacked = (
        f"; none backs any of its {len(required)} required skills"
        if required and not best_cover
        else ""
    )
    if margin < MIN_MARGIN and profile.base_resume_id is not None:
        standing = next((pair for pair in leaders if pair[1].id == profile.base_resume_id), None)
        if standing is not None:
            like = f", which backs {of} like the best match" if best_cover else ""
            reason = (
                f"kept the profile's own résumé{like} — the best match won by only "
                f"{margin:.3f}, which is inside the noise{unbacked}"
            )
            return _choice(standing[1], standing[0], reason, ranked, required)
    reason = (
        f"closest of {len(leaders)} résumés{backing} to this posting "
        f"({best_score:.3f}, next {runner_up:.3f}){unbacked}"
    )
    return _choice(best, best_score, reason, ranked, required)
