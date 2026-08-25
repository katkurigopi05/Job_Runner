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

import html
import re
import uuid

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Project, Resume
from packages.core.storage import cover_letter_key, get_storage, resume_key
from packages.tailor.assemble import AssemblyOptions, assemble_pdf
from packages.tailor.parse import ParsedResume
from packages.tailor.rewrite import TailorResult
from packages.tailor.skills import reorder_skills

log = structlog.get_logger(__name__)

#: The section re-emphasized against the posting. Named here so it cannot
#: drift from packages/tailor/parse.py::SECTION_PATTERNS.
SKILLS_SECTION = "skills"

#: The section the tailor rewrites. Kept here rather than inlined so this and
#: the caller that extracted the bullets cannot drift apart.
TAILORED_SECTION = "experience"


def apply_rewrites(
    parsed: ParsedResume, result: TailorResult, *, posting_text: str = ""
) -> ParsedResume:
    """A new résumé with the vetted rewrites substituted in.

    Returns a copy — the parsed source is the record of what the owner
    actually wrote and must survive tailoring unchanged, or the guard loses
    the thing it checks against.

    With `posting_text`, the Skills section is also re-emphasized: the skills
    the posting names move to the front of their own line. Order only — see
    `packages.tailor.skills`, which cannot add or remove one. `raw_lines` is
    deliberately left alone, because it is what the guard treats as "was this
    in the source" and reordering is not a change to that answer.
    """
    lines = parsed.section(TAILORED_SECTION)
    if not lines:
        return _with_ordered_skills(parsed.model_copy(deep=True), posting_text)

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
    return _with_ordered_skills(tailored, posting_text)


def _with_ordered_skills(resume: ParsedResume, posting_text: str) -> ParsedResume:
    """Re-emphasize the Skills section in place on an already-copied résumé."""
    if not posting_text.strip():
        return resume
    existing = resume.sections.get(SKILLS_SECTION)
    if not existing:
        return resume
    resume.sections[SKILLS_SECTION] = reorder_skills(existing, posting_text)
    return resume


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
    posting_text: str = "",
    options: AssemblyOptions | None = None,
    tailored_key: str | None = None,
    posting_id: uuid.UUID | None = None,
    answered_by: str | None = None,
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
    tailored = apply_rewrites(parsed, result, posting_text=posting_text)

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
        # Written only on the miss that produced this row. Left NULL when the
        # caller had nothing safe to key on, which keeps it out of every future
        # lookup rather than making it reusable by accident.
        tailored_key=tailored_key,
        # Independent of the key above: `tailored_key` decides reuse and is a
        # digest, this answers "which job was this written for" and is
        # readable. A posting with no content hash is uncacheable but still
        # perfectly nameable, so this is set even when the key is not.
        tailored_for_posting_id=posting_id,
        # Read off the provider *after* the rewrites, so a run that fell back to
        # the local model records the model that answered rather than the one
        # that was asked. Left NULL when the caller did not say: unrecorded is
        # an honest answer, a guessed model name is not.
        tailored_by=answered_by,
    )
    session.add(resume)
    await session.flush()

    log.info(
        "tailored_resume_published",
        resume_id=str(resume.id),
        version=version,
        bytes=len(pdf),
        rewritten=result.changed_count,
        tailored_by=answered_by,
    )
    return resume


#: A letter is prose, not a résumé — no rules, no small caps, wider leading.
#: Same face and page box as RESUME_CSS so the two documents an employer opens
#: together do not look like they came from different people.
LETTER_CSS = """
@page { size: Letter; margin: 1in; }
body { font-family: "DejaVu Sans", Helvetica, Arial, sans-serif;
       font-size: 10.5pt; line-height: 1.5; color: #111; }
p { margin: 0 0 10pt; }
"""


def render_cover_letter(text: str) -> bytes:
    """The letter as a PDF, laid out one paragraph per blank-line block."""
    from weasyprint import CSS, HTML

    paragraphs = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    body = "\n".join(
        # Single newlines inside a block are the letter's own line breaks —
        # a signature block is three lines and one paragraph.
        "<p>" + "<br/>".join(html.escape(line) for line in block.splitlines()) + "</p>"
        for block in paragraphs
    )
    document = HTML(string=f"<body>{body}</body>")
    return bytes(document.write_pdf(stylesheets=[CSS(string=LETTER_CSS)]))


def publish_cover_letter(text: str, *, application_id: str) -> str | None:
    """Store the letter and return its storage ref, or None if it could not be.

    Two formats, and the fallback is the point. The PDF is what gets uploaded
    when the employer offers "Attach"; the `.txt` is what survives a machine
    without Pango, where WeasyPrint raises. A form that offers a textarea
    needs neither — the text goes straight into the field — so losing the
    render must not lose the letter.

    Returns None only when storage itself failed, which is the one case where
    there is nothing to point `Application.cover_letter_ref` at.
    """
    storage = get_storage()

    try:
        pdf = render_cover_letter(text)
    except Exception as exc:  # noqa: BLE001 - a render failure must not lose the letter
        log.warning("cover_letter_render_failed", error=type(exc).__name__)
        key = cover_letter_key(application_id, "cover-letter.txt")
        payload = text.encode("utf-8")
    else:
        key = cover_letter_key(application_id, "cover-letter.pdf")
        payload = pdf

    try:
        storage.put(key, payload)
    except Exception as exc:  # noqa: BLE001 - same reasoning as the render
        log.warning("cover_letter_store_failed", error=type(exc).__name__)
        return None

    log.info("cover_letter_published", key=key, bytes=len(payload))
    return key
