"""Re-score the feed against the profile as it stands now — `make rescore`.

Run this after changing anything the score is computed *from*: a new résumé,
a new base résumé on the profile, an edited profile. Crawling re-scores too,
but only when the sweep found something, so after a résumé upload it usually
does not run at all.

    make rescore                  # every profile
    make rescore p=backend        # one
    make rescore dry=1            # show the change, write nothing
    make rescore re=1             # re-encode every posting first

Use `re=1` after changing EMBEDDING_BACKEND. Stored vectors are reused
otherwise, so a backend switch would leave the old backend's vectors in the
table and rank the new profile encoding against them.
"""

from __future__ import annotations

import argparse
import asyncio

from packages.core import db as core_db
from packages.matching.rescore import ProfileRescore, rescore


def _show(entry: ProfileRescore) -> None:
    print(f"\n{entry.label} — top {len(entry.after_top)}")
    before = {title: score for score, title in entry.before_top}
    for position, (score, title) in enumerate(entry.after_top, start=1):
        was = before.get(title)
        # A title that was not in the old top N is the interesting case: the
        # re-score promoted it from somewhere below.
        delta = "  (new to top)" if was is None else f"  (was {was:.3f})"
        print(f"  {position:>2}. {score:.3f}  {title[:64]}{delta}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=None, help="profile label; default is all of them")
    parser.add_argument("--dry-run", action="store_true", help="score and report, then roll back")
    parser.add_argument(
        "--re-embed",
        action="store_true",
        help="recompute every posting vector — required after an EMBEDDING_BACKEND change",
    )
    args = parser.parse_args()

    async with core_db.get_sessionmaker()() as session:
        report = await rescore(session, label=args.profile, re_embed=args.re_embed)
        print(report.summary())

        for entry in report.profiles:
            _show(entry)

        if args.dry_run:
            await session.rollback()
            print("\ndry run — nothing written")
            return

        await session.commit()
        print("\nwritten")


if __name__ == "__main__":
    asyncio.run(main())
