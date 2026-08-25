"""Owner edits to a parsed résumé.

The résumé arrives as a file and is parsed once at upload. Until now that was
the only way to change it: fixing a typo, or a section the parser mis-split,
meant editing the source document elsewhere and re-uploading. This is the
smaller path — edit the parsed form directly.

Two things make it more than a form save.

## `raw_lines` has to be rebuilt

`raw_lines` is what the fabrication guard treats as "was this in the source"
(`packages/tailor/publish.py::apply_rewrites` says so explicitly, and leaves it
alone precisely because reordering is not a change to that answer).

An owner edit *is* a change to that answer. A résumé edited to add a real
employer, with `raw_lines` left at the parsed original, would have the guard
refuse the owner's own new fact during tailoring — the rewriter would look
broken while behaving exactly as designed. So an edit regenerates it.

This does mean editing widens what tailoring may say. That is correct and is
the point: the guard's job is to stop the *model* inventing, not to stop the
owner writing their own résumé.

## The edit is a new version, never a mutation

An `Application` may already point at this résumé and may already have sent it.
Rewriting the row in place would make the receipt describe a document that no
longer exists, which is the one thing an audit trail must not do. The caller
creates a new `Resume` row; this module only builds the value.
"""

from __future__ import annotations

from packages.tailor.assemble import SECTION_ORDER
from packages.tailor.parse import Contact, ParsedResume


def _ordered_sections(sections: dict[str, list[str]]) -> list[tuple[str, list[str]]]:
    """Known sections in document order, then anything the parser invented.

    Unknown keys are kept rather than dropped. A parser that produced a section
    this list has never heard of is still holding the owner's text, and losing
    it on save would be a silent deletion of their own résumé.
    """
    known = [(name, sections[name]) for name in SECTION_ORDER if name in sections]
    extra = sorted((name, lines) for name, lines in sections.items() if name not in SECTION_ORDER)
    return known + extra


def rebuild_raw_lines(resume: ParsedResume) -> ParsedResume:
    """A copy whose `raw_lines` agrees with its contact and sections.

    Order follows `SECTION_ORDER` so the flattened text reads like a résumé
    rather than like a dict. Blank lines are preserved inside a section — they
    are the owner's paragraph breaks — but not invented between them.
    """
    edited = resume.model_copy(deep=True)

    lines: list[str] = []
    contact = edited.contact
    for value in (contact.name, contact.email, contact.phone):
        if value and value.strip():
            lines.append(value.strip())
    lines.extend(link.strip() for link in contact.links if link.strip())

    lines.extend(line for line in edited.preamble if line.strip())

    for name, section_lines in _ordered_sections(edited.sections):
        kept = [line for line in section_lines if line.strip()]
        if not kept:
            continue
        # The heading itself goes in. The parser put one there, so a corpus
        # rebuilt without it would stop recognising the word "Experience" as
        # having been in the source.
        lines.append(name)
        lines.extend(kept)

    edited.raw_lines = lines
    return edited


def apply_edit(
    source: ParsedResume, *, contact: Contact, sections: dict[str, list[str]]
) -> ParsedResume:
    """The owner's edit, as a résumé ready to render and store.

    Takes the source rather than building from nothing so anything the editor
    does not expose — `preamble` today — survives the round trip instead of
    being silently dropped by a form that never showed it.

    Empty sections are removed. A section the owner cleared is one they meant
    to delete, and keeping an empty key would print a bare heading onto the PDF.
    """
    edited = source.model_copy(deep=True)
    edited.contact = contact
    edited.sections = {
        name: [line for line in lines if line.strip()]
        for name, lines in sections.items()
        if any(line.strip() for line in lines)
    }
    return rebuild_raw_lines(edited)
