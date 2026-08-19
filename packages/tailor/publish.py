"""Turn a set of vetted rewrites into a file the owner can actually upload.

Tailoring already worked before this module existed — `tailor_bullets` ran,
the guard vetted every rewrite, and a diff went onto the review screen. What
never happened was the last step: nothing rendered the result, so
`Application.tailored_resume_id` stayed null on every row ever written and the
owner finishing an application by hand had the *original* résumé to upload.
A rewrite nobody can attach is a rewrite that did not happen.

**Why this is safe to render.** `tailor_bullet` returns the source line
verbatim whenever the guard refuses a rewrite — rejection is a fallback, not a
flag. So every line in a `TailorResult` is either an approved rewrite or the
original text, and there is no third case. That property, not any check here,
is what lets this write a PDF without re-litigating §2.1.

**Why the rewrites are re-applied positionally.** The bullets handed to the
tailor were the non-blank lines of the experience section, in order. Matching
the results back by string equality would collapse two identical bullets under
different employers into one, so the walk here consumes results in the same
order they were produced and leaves blank lines untouched.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Project, Resume
from packages.core.storage import get_storage, resume_key
from packages.tailor.assemble import AssemblyOptions, assemble_pdf
from packages.tailor.parse import ParsedResume
from packages.tailor.rewrite import TailorResult

log = structlog.get_logger(__name__)

#: The section the tailor rewrites. Kept here rather than inlined so this and
#: the caller that extracted the bullets cannot drift apart.
TAILORED_SECTION = "experience"


def apply_rewrites(parsed: ParsedResume, result: TailorResult) -> ParsedResume:
    """A new résumé with the vetted rewrites substituted in.

    Returns a copy — the parsed source is the record of what the owner
    actually wrote and must survive tailoring unchanged, or the guard loses
    the thing it checks against.
    """
    lines = parsed.section(TAILORED_SECTION)
    if not lines:
        return parsed.model_copy(deep=True)

    pending = iter(result.bullets)
    rewritten: list[str] = []
    for line in lines:
        if not line.strip():
            rewritten.append(line)
            continue
        bullet = next(pending, None)
        # Falling back to the source line covers the case where the result is
        # shorter than the section — a truncated result must never shift every
        # later bullet onto the wrong employer.
        rewritten.append(bullet.tailored if bullet is not None else line)

    tailored = parsed.model_copy(deep=True)
    tailored.sections[TAILORED_SECTION] = rewritten
    return tailored


async def _next_version(session: AsyncSession, candidate_id: uuid.UUID) -> int:
    highest = await session.scalar(
        select(func.max(Resume.version)).where(Resume.candidate_id == candidate_id)
    )
    return int(highest or 0) + 1


async def publish_tailored(
    session: AsyncSession,
    *,
    candidate_id: uuid.UUID,
    parsed: ParsedResume,
    result: TailorResult,
    projects: list[Project] | None = None,
    options: AssemblyOptions | None = None,
) -> Resume | None:
    """Render the tailored résumé to PDF, store it, and return its row.

    Returns None rather than raising if rendering fails. WeasyPrint needs
    system libraries (Pango, cairo) that a given machine may not have, and an
    application that reached this point has a filled form and a screenshot
    waiting — losing that over a missing font library would be the worse
    outcome. The failure is logged and the owner uploads the base résumé.

    Not committed here. The caller owns the transaction, so the new résumé and
    the application row that points at it land together or not at all.
    """
    tailored = apply_rewrites(parsed, result)

    try:
        pdf = assemble_pdf(tailored, projects, options)
    except Exception as exc:  # noqa: BLE001 - a render failure must not fail the run
        log.warning("tailored_render_failed", error=type(exc).__name__)
        return None

    version = await _next_version(session, candidate_id)
    key = resume_key(str(candidate_id), version, "tailored.pdf")

    try:
        storage = get_storage()
        storage.put(key, pdf)
    except Exception as exc:  # noqa: BLE001 - same reasoning as the render
        log.warning("tailored_store_failed", error=type(exc).__name__)
        return None

    resume = Resume(
        candidate_id=candidate_id,
        version=version,
        storage_ref=key,
        # The parsed form of what was rendered, so a later tailoring pass reads
        # the tailored text rather than re-deriving it from the PDF.
        parsed_json=tailored.model_dump(mode="json"),
        is_default=False,
    )
    session.add(resume)
    await session.flush()

    log.info(
        "tailored_resume_published",
        resume_id=str(resume.id),
        version=version,
        bytes=len(pdf),
        rewritten=result.changed_count,
    )
    return resume
