"""Storing an owner's edit to a résumé as a new version.

`edit.py` builds the edited value and deliberately stops there — it is pure, and
says so. This is the other half: render the edit to a PDF, store the file, and
write the new `Resume` row.

It exists as a module rather than as code inside a route because there are now
**two** places an owner edits a résumé, and they must not drift apart:

- `/resumes/{id}/edit` — the document itself, on the résumés page.
- `/applications/{id}/resume/edit` — the document about to be sent, on the
  review screen.

Both have to render a file, rebuild `raw_lines`, and version rather than mutate.
A second copy of that sequence is how one of them ends up storing `parsed_json`
without a file, which is invisible until an employer receives the old PDF. The
divergence between "what the review screen shows" and "what gets uploaded" is
the defect this project has already had once (CLAUDE.md §15), and one shared
function is the cheapest way not to have it twice.

What this module does **not** do is decide what the edit is *for*. Adoption,
attaching to an application, and the pin that keeps the edit alive through a
resumed run are the callers' business, because they differ per caller.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Resume
from packages.core.storage import get_storage, resume_key
from packages.tailor.edit import apply_edit
from packages.tailor.parse import Contact, ParsedResume


class ReviseError(Exception):
    """An edit that could not be saved.

    `internal` separates "the owner sent something unusable" from "this machine
    could not render it" — the same split the error envelope makes, decided here
    where the cause is known rather than guessed at by the route.
    """

    def __init__(self, message: str, *, internal: bool = False) -> None:
        super().__init__(message)
        self.internal = internal


async def save_edit(
    session: AsyncSession,
    source: Resume,
    *,
    contact: Contact,
    sections: dict[str, list[str]],
) -> Resume:
    """The owner's edit, rendered and stored as a new version of `source`.

    Three properties, each of which is a silent wrong answer if skipped.

    **A new version, never a mutation.** An `Application` may already point at
    the source and may already have sent it. Rewriting that row in place would
    leave a receipt describing a document that no longer exists.

    **The PDF is re-rendered from the edit.** `apply_job._resume_path` uploads
    the *file*, while tailoring renders from `parsed_json`. Storing the edit
    without a new file would make it invisible on untailored applications and
    visible on tailored ones.

    **`raw_lines` is rebuilt** (in `edit.py`). It is what the fabrication guard
    treats as "was this in the source", so an edit that added a real employer
    while leaving it stale would have the guard refuse the owner's own fact.

    The row is flushed, not committed. The caller has more to say about this
    edit — which profiles adopt it, which application attaches it — and those
    belong in the same transaction as the row itself.
    """
    if not source.parsed_json:
        raise ReviseError("this résumé has no parsed form to edit")

    # Emptiness is judged on what the editor actually controls, not on the
    # result. `preamble` survives the round trip because no form shows it, so a
    # cleared résumé could still render a PDF containing nothing but a stray
    # "Available from June" — technically non-empty, and not a document anyone
    # meant to send.
    has_contact = any(bool(value) for value in (contact.name, contact.email, contact.phone))
    has_links = any(link.strip() for link in contact.links)
    has_sections = any(line.strip() for lines in sections.values() for line in lines)
    if not (has_contact or has_links or has_sections):
        raise ReviseError("the edited résumé is empty")

    parsed = ParsedResume.model_validate(source.parsed_json)
    edited = apply_edit(parsed, contact=contact, sections=sections)

    from packages.tailor.assemble import assemble_pdf

    try:
        # Projects are excluded here. They are rebuilt per posting at tailoring
        # time from the GitHub inventory, so baking today's set into a stored
        # document would freeze a section that is supposed to follow the job.
        pdf = assemble_pdf(edited, None, None)
    except Exception as exc:  # noqa: BLE001 - WeasyPrint needs system libraries
        raise ReviseError(
            f"the edit could not be rendered to PDF ({type(exc).__name__}), so it was not "
            "saved — a stored edit with no file would be invisible to every application",
            internal=True,
        ) from exc

    next_version = (
        await session.scalar(
            select(func.coalesce(func.max(Resume.version), 0) + 1).where(
                Resume.candidate_id == source.candidate_id
            )
        )
    ) or 1

    storage = get_storage()
    key = resume_key(str(source.candidate_id), next_version, "resume.pdf")
    try:
        storage.put(key, pdf)
    except Exception as exc:  # noqa: BLE001 - surfaces size limits too
        raise ReviseError(f"could not store the edit: {exc}", internal=True) from exc

    resume = Resume(
        candidate_id=source.candidate_id,
        version=next_version,
        storage_ref=key,
        parsed_json=edited.model_dump(mode="json"),
        is_default=source.is_default,
        # Deliberately not carried over from `source`. `tailored_key` is a
        # cache key over the inputs that produced a document, and an owner's
        # edit is not one of them — inheriting it would serve this hand-edited
        # résumé to a later application that asked for a machine-tailored one.
        # `tailored_by` is dropped for the same reason: the model wrote the
        # document this was derived from, not this one.
        tailored_for_posting_id=source.tailored_for_posting_id,
    )
    session.add(resume)
    await session.flush()
    return resume


def uuid_or_none(value: str | None) -> uuid.UUID | None:
    """Parse an id that came out of a JSON blob rather than off a column."""
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def guard_edit(source: Resume, sections: dict[str, list[str]]) -> None:
    """Hold a proposed edit to the fabrication guard. Raises on a violation.

    Not applied to the dashboard editor, and that asymmetry is the point. §2.1
    constrains the *model*, not the owner writing their own history — `edit.py`
    records exactly that, and the editor is a person at a keyboard who is the
    authority on their own employers and dates.

    A tool call is different. There the author is a model, and an unguarded
    résumé write handed to one is the door §2.1 exists to close: it would let an
    assistant put an employer, a credential, or a metric onto a document going
    to a real employer under the owner's name, with no check anywhere. The
    system prompt can ask a model not to; CLAUDE.md §14 is explicit that a
    prompt is a request and the check belongs in code.

    Only *new* lines are checked. A line carried over unchanged came from the
    source by definition, and re-checking it would refuse edits that touched
    nothing — the guard is deliberately strict enough that some genuine source
    text does not survive a round trip through it.

    The check is document-wide rather than scoped to one employer's entry. A
    scoped check is what `rewrite.py` does to a *rewritten bullet*, where the
    claim being made is "the owner did this **here**". An added line has no
    entry to be scoped against yet, so what is enforceable is the weaker and
    still useful question: does this fact appear in the résumé at all. Cross-
    entry borrowing is not caught here, and callers should not assume it is.
    """
    from packages.tailor.guard import SourceCorpus, check

    parsed = ParsedResume.model_validate(source.parsed_json or {})
    corpus = SourceCorpus.from_resume(parsed)

    known = {line.strip() for lines in parsed.sections.values() for line in lines}
    known |= {line.strip() for line in parsed.raw_lines}

    offenders: list[str] = []
    for lines in sections.values():
        for line in lines:
            text = line.strip()
            if not text or text in known:
                continue
            report = check(text, corpus)
            if not report.ok:
                offenders.append(f"{text!r} — {report.summary()}")

    if offenders:
        listed = "; ".join(offenders[:3])
        more = "" if len(offenders) <= 3 else f" (+{len(offenders) - 3} more)"
        raise ReviseError(
            "refused: this edit adds claims the résumé does not support. §2.1 permits "
            "rephrasing and reordering, not new facts. If these are true, the owner "
            "should type them on the /review screen themselves — an edit made there is "
            f"theirs and is not guarded. Offending lines: {listed}{more}"
        )
