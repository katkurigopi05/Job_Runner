"""Re-derive `parsed_json` from the stored file, for résumés parsed by older code.

    python -m scripts.reparse_resumes            # report only
    python -m scripts.reparse_resumes --write    # apply

`parsed_json` is a *derivation* of the uploaded file, not a second document.
When the extractor improves, every row written before it keeps the old
reading — and nothing notices, because the row is self-consistent and the file
it came from has not changed.

That is not hypothetical. `_paragraph_text` was fixed to stop fusing DOCX
fields across run boundaries, which is what turned the owner's education lines
into `Master of Science in Business AnalyticsJan 2025 – Dec 2026`. An ATS
reads `AnalyticsJan` as one token: the degree is wrong and the start date is
not there at all. The fix landed; the stored row kept the fused text and kept
scoring 82% where the same file re-read scores 96%.

## Why this rewrites in place rather than versioning

Everything else that changes a résumé here versions instead of mutating,
because it changes the *document* — an edit on `/review`, a tailoring pass.
This changes nothing about the document. The bytes in storage are untouched
and the file an employer receives is the same file; only our reading of it is
corrected. A new version would claim the owner's résumé had changed, put a
`v7` in front of them they never wrote, and leave the wrong reading behind as
though it were a legitimate earlier draft.

## What it will not do

It never reparses a row whose file is missing, and it never writes a parse
that is *worse* than the stored one. A regression in the extractor would
otherwise be applied silently and irreversibly across every résumé at once,
which is exactly the shape of damage `validate-seeds` caused when it retired
live boards. A row that got worse is reported instead, and left alone.
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from packages.core.db import get_sessionmaker
from packages.core.models import Resume
from packages.core.storage import get_storage
from packages.tailor import ats
from packages.tailor.parse import ParsedResume, ParseError, extract_text, parse_text


def _stored_parse(resume: Resume) -> float | None:
    """The parse score of what is currently in the database."""
    if not resume.parsed_json:
        return None
    try:
        return ats.score(ParsedResume.model_validate(resume.parsed_json), "").parse
    except Exception:  # noqa: BLE001 - a malformed row is exactly what this fixes
        return None


async def run(*, write: bool) -> int:
    storage = get_storage()
    changed = 0
    worse = 0

    async with get_sessionmaker()() as session:
        resumes = (await session.scalars(select(Resume).order_by(Resume.version))).all()

        for resume in resumes:
            label = f"v{resume.version}"

            # A tailored row's `parsed_json` is the structure the tailorer
            # authored; the stored PDF is a *render* of it. Re-reading the
            # render is a lossy round trip — on the owner's own v3 and v4 it
            # scored 82% down to 75% — and it would also silently discard the
            # rewrite, since what came back is whatever the PDF text layer
            # gives rather than what the guard approved.
            #
            # The "never write worse" rule below would have caught both, but
            # only by accident of the numbers. This is the actual reason.
            if resume.tailored_for_posting_id is not None:
                print(f"{label:5} tailored — not reparsed")
                continue

            if not resume.storage_ref:
                print(f"{label:5} no stored file — skipped")
                continue

            try:
                data = storage.get(resume.storage_ref)
            except Exception as exc:  # noqa: BLE001 - a missing file is not fatal
                print(f"{label:5} could not read {resume.storage_ref}: {type(exc).__name__}")
                continue

            filename = resume.storage_ref.rsplit("/", 1)[-1]
            try:
                parsed = parse_text(extract_text(data, filename))
            except ParseError as exc:
                print(f"{label:5} will not parse: {exc}")
                continue

            before = _stored_parse(resume)
            after = ats.score(parsed, "").parse

            if before is not None and after < before:
                # Reported, never applied. See the module docstring.
                print(f"{label:5} {before:.0%} -> {after:.0%}  WORSE — left alone")
                worse += 1
                continue

            if before is not None and abs(after - before) < 1e-9:
                print(f"{label:5} {after:.0%} unchanged")
                continue

            shown = "none" if before is None else f"{before:.0%}"
            print(f"{label:5} {shown} -> {after:.0%}")
            changed += 1
            if write:
                resume.parsed_json = parsed.model_dump()

        if write and changed:
            await session.commit()

    print()
    if write:
        print(f"rewrote {changed} résumé(s).")
    else:
        print(f"{changed} would change, {worse} would get worse. Re-run with --write to apply.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="apply the re-parse; without it nothing is written",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(write=args.write)))


if __name__ == "__main__":
    main()
