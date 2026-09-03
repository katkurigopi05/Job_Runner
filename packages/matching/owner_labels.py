"""Turn the owner's graded postings into a labeled set — `Provenance.OWNER`.

The sibling of `feedback.py`, and the distinction between them is the point of
`docs/BACKLOG.md` P1.

`feedback.py` infers a grade from a swipe: real evidence, but binary, and only
ever collected on postings the ranker already surfaced. This exports grades the
owner sat down and gave on the 0–3 scale, on postings drawn deliberately —
including ones the ranker buried. That is what `OWNER` means, and why
`bench_matching` may stop calling a corpus fixture-only once these exist.

## The report is about coverage, not just count

A hundred `OWNER` labels that are all `uncertain`-stream have the shortlist
problem back, wearing the provenance a benchmark trusts most. So the summary
reports the stream mix and the grade spread, and says so when either collapses.
A count on its own cannot tell a usable corpus from a self-confirming one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Posting, PostingLabel, Profile
from packages.matching.labels import RELEVANCE_SCALE, LabeledPosting, LabeledSet, Provenance

#: `docs/BACKLOG.md` P1's "done when": below this the set cannot support a
#: trained model, and `ML_EVALUATION.md` keeps refusing to name a candidate.
TARGET_LABELS = 100

#: Below this no ranking metric means anything, whatever the provenance.
MIN_USEFUL_LABELS = 20


@dataclass(frozen=True)
class OwnerReport:
    profile: str
    total: int
    grades: dict[int, int] = field(default_factory=dict)

    @property
    def graded_classes(self) -> int:
        return sum(1 for count in self.grades.values() if count)

    def summary(self) -> str:
        spread = ", ".join(f"{g}:{self.grades.get(g, 0)}" for g in sorted(RELEVANCE_SCALE))
        lines = [f"{self.profile}: {self.total} owner-graded postings ({spread})"]

        if self.total < MIN_USEFUL_LABELS:
            lines.append(
                f"  {self.total} is below the {MIN_USEFUL_LABELS} a ranking metric needs "
                "to say anything."
            )
        elif self.total < TARGET_LABELS:
            lines.append(
                f"  {TARGET_LABELS - self.total} short of the {TARGET_LABELS} "
                "docs/BACKLOG.md P1 asks for before a trained model is worth fitting."
            )

        if self.graded_classes < 2:
            lines.append(
                "  One grade only — every metric is degenerate until the corpus "
                "disagrees with itself somewhere."
            )
        return "\n".join(lines)


def _key(posting: Posting) -> str:
    """A stable, readable id, prefixed so provenance is legible in the file.

    `feedback.py` uses `fb-` for the same reason: two exports can land in one
    corpus, and a key that does not say where it came from makes a mixed set
    unreadable at the point where that matters most.
    """
    raw = posting.external_id or str(posting.id)
    return "ow-" + re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")[:48]


async def export_owner_labels(
    session: AsyncSession,
    profile: Profile,
    *,
    name: str = "owner-graded",
    version: str = "v1",
) -> tuple[LabeledSet | None, OwnerReport]:
    """Build a `LabeledSet` from every grade this profile carries.

    Returns `None` for the set when nothing has been graded — an empty corpus
    is not a corpus, and `load_labeled_set` would refuse it anyway.
    """
    rows = (
        await session.execute(
            select(PostingLabel, Posting)
            .join(Posting, Posting.id == PostingLabel.posting_id)
            .where(PostingLabel.profile_id == profile.id)
            .order_by(PostingLabel.created_at)
        )
    ).all()

    items: list[LabeledPosting] = []
    grades: dict[int, int] = dict.fromkeys(RELEVANCE_SCALE, 0)

    for label, posting in rows:
        grades[label.relevance] = grades.get(label.relevance, 0) + 1
        items.append(
            LabeledPosting(
                key=_key(posting),
                title=posting.title or "",
                description=posting.description_raw or "",
                relevance=label.relevance,
                provenance=Provenance.OWNER,
                company="",
                location=posting.location or "Remote",
                note=label.note or "",
                tags=("owner",),
            )
        )

    report = OwnerReport(profile=profile.label, total=len(items), grades=grades)
    if not items:
        return None, report

    return (
        LabeledSet(
            name=name,
            version=version,
            profile_text=profile.label,
            items=tuple(items),
            description=(
                "Graded by the owner on the 0-3 scale in /label. Drawn across "
                "uncertain, unseen and confident streams — see "
                "packages/matching/active.py for why the unseen stream is what "
                "separates these from swipe-derived labels."
            ),
        ),
        report,
    )
