"""Turn a real mail export into a Gate 6 measurement — `make import-mail`.

    make import-mail src=~/mail.mbox        # build a worksheet
    make score-mail  ws=seeds/mail_labels.yaml   # score what you labeled

Gate 6 asks for 30 hand-labeled **real** recruiter emails. There are 30
written to match, `inbound_messages` is 0, and the fixtures were written
beside the patterns that read them — so the number the gate reports has never
been computed against a message a recruiter actually sent.

Export the recruiter mail from Gmail (Takeout, or drag a label to a folder as
`.eml`), point this at it, fill in the `label` column, and score. The number
that comes out is the one Gate 6 was written to ask for.

Nothing is uploaded anywhere: the worksheet is a local file and the classifier
tier that runs here is the rules, which are pure regex.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from packages.core.enums import Classification
from packages.inbox.classify import RuleClassifier
from packages.inbox.importer import (
    build_worksheet,
    read_export,
    read_worksheet,
    score_worksheet,
    write_worksheet,
)

DEFAULT_WORKSHEET = Path("seeds/mail_labels.yaml")


def _guess(subject: str, body: str) -> Classification | None:
    verdict = RuleClassifier().classify(subject, body)
    return None if verdict.abstained else verdict.classification


def _classify(subject: str, body: str) -> Classification:
    verdict = RuleClassifier().classify(subject, body)
    return Classification.NOISE if verdict.abstained else verdict.classification


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", help="an .mbox, an .eml, or a directory of either")
    parser.add_argument("--out", default=str(DEFAULT_WORKSHEET), help="worksheet to write")
    parser.add_argument("--score", help="score an already-labeled worksheet instead")
    args = parser.parse_args()

    if args.score:
        report = score_worksheet(read_worksheet(Path(args.score)), _classify)
        print(report.summary())
        return

    if not args.src:
        parser.error("pass --src <mbox|eml|dir> to build a worksheet, or --score <worksheet>")

    messages, report = read_export(Path(args.src).expanduser())
    print(report.summary())
    if not messages:
        return

    guesses = [_guess(m.subject, m.body) for m in messages]
    path = write_worksheet(build_worksheet(messages, guesses), Path(args.out))
    print(f"\nwrote {path}")
    print("Fill in `label` on each row, then:")
    print(f"  make score-mail ws={path}")


if __name__ == "__main__":
    asyncio.run(main())
