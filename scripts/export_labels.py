"""Export the owner's swipe decisions as a labeled set — `make export-labels`.

    make export-labels                     # every profile with decisions
    make export-labels p=backend           # one
    make export-labels out=seeds/mine.yaml # somewhere other than the default

Gate 5 asks whether the ranker works on *this owner's* material, and every
label in the repo is a fixture written beside the code that reads it. The
judgements needed to answer it are already being collected — `/swipe` records
one every time the owner says yes or no to a posting — and nothing has ever
read them back out. This does.

What comes out is `Provenance.FEEDBACK`, not `OWNER`, and
`packages/matching/feedback.py` explains why that matters: a swipe is binary
and is only ever taken on postings the ranker already surfaced. Real evidence,
narrower than a graded label.
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

DEFAULT_OUT = Path("seeds/owner_feedback.yaml")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=None, help="profile label; default is all of them")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="where to write the labeled set")
    args = parser.parse_args()

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
            labeled, report = await export_decisions(session, profile)
            print(report.summary())
            if labeled is None:
                continue

            # One file per profile once there is more than one, because a
            # labeled set is defined against a single profile_text and merging
            # two of them would silently average two different people's taste.
            out = Path(args.out)
            if len(profiles) > 1:
                out = out.with_name(f"{out.stem}-{profile.label}{out.suffix}")
            dump_labeled_set(labeled, out)
            print(f"  wrote {out}")
            written += 1

        if not written:
            print("\nNothing decided yet — swipe some matches at /swipe first.")
            return

        print("\nRun the benchmark against it with:")
        print('  make bench-matching ARGS="--set seeds/owner_feedback.yaml"')


if __name__ == "__main__":
    asyncio.run(main())
