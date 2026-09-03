"""Export the owner's relevance judgements as a labeled set — `make export-labels`.

    make export-labels                     # every profile with judgements
    make export-labels p=backend           # one
    make export-labels out=seeds/mine.yaml # somewhere other than the default
    make export-labels kind=owner          # graded in /label, not swiped

Two kinds, and they are deliberately separate files with separate provenance.

Gate 5 asks whether the ranker works on *this owner's* material, and every
label in the repo is a fixture written beside the code that reads it. The
judgements needed to answer it are already being collected — `/swipe` records
one every time the owner says yes or no to a posting — and nothing has ever
read them back out. This does.

`kind=feedback` (the default) comes out as `Provenance.FEEDBACK`, and
`packages/matching/feedback.py` explains why that matters: a swipe is binary
and is only ever taken on postings the ranker already surfaced. Real evidence,
narrower than a graded label.

`kind=owner` exports the 0-3 grades from `/label` as `Provenance.OWNER`. Those
are drawn across three streams — including postings the ranker never scored —
which is what makes them the grade `bench_matching` will stop calling
fixture-only. `packages/matching/active.py` says why that stream is the whole
difference.

They are never merged into one file. A set is defined against one profile and
one kind of evidence; averaging a graded judgement with an inferred one would
lose the distinction the provenance exists to carry.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from sqlalchemy import select

from packages.core import db as core_db
from packages.core.models import Profile
from packages.matching.feedback import export_decisions
from packages.matching.labels import dump_labeled_set
from packages.matching.owner_labels import export_owner_labels

DEFAULT_OUT = Path("seeds/owner_feedback.yaml")
DEFAULT_OWNER_OUT = Path("seeds/owner_graded.yaml")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=None, help="profile label; default is all of them")
    parser.add_argument("--out", default=None, help="where to write the labeled set")
    parser.add_argument(
        "--kind",
        choices=["feedback", "owner"],
        default="feedback",
        help="feedback: swipe decisions. owner: 0-3 grades from /label.",
    )
    args = parser.parse_args()
    owner = args.kind == "owner"
    out_default = DEFAULT_OWNER_OUT if owner else DEFAULT_OUT

    async with core_db.get_sessionmaker()() as session:
        query = select(Profile)
        if args.profile:
            query = query.where(Profile.label == args.profile)
        profiles = (await session.scalars(query)).all()

        if not profiles:
            print("no profiles found")
            return

        written = 0
        for profile in profiles:
            if owner:
                labeled, report = await export_owner_labels(session, profile)  # type: ignore[assignment]
            else:
                labeled, report = await export_decisions(session, profile)  # type: ignore[assignment]
            print(report.summary())
            if labeled is None:
                continue

            # One file per profile once there is more than one, because a
            # labeled set is defined against a single profile_text and merging
            # two of them would silently average two different people's taste.
            out = Path(args.out or out_default)
            if len(profiles) > 1:
                out = out.with_name(f"{out.stem}-{profile.label}{out.suffix}")
            dump_labeled_set(labeled, out)
            print(f"  wrote {out}")
            written += 1

        if not written:
            print(
                "\nNothing graded yet — grade some postings at /label first."
                if owner
                else "\nNothing decided yet — swipe some matches at /swipe first."
            )
            return

        print("\nRun the benchmark against it with:")
        print(f'  make bench-matching ARGS="--set {out_default}"')


if __name__ == "__main__":
    asyncio.run(main())
