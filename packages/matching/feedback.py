"""Turn the owner's swipe decisions into a labeled set.

`Provenance.OWNER` and `Provenance.FEEDBACK` have existed since `labels.py`
was written and nothing has ever produced one. Every label in the repo is
`FIXTURE` — written beside the code that reads it — which is why
`bench_matching` refuses to report a production candidate and why CLAUDE.md
§15 says Gate 5 does not answer the question it was written to ask.

The data to answer it is already being collected. `/swipe` writes
`Match.decision`, and a swipe *is* a relevance judgement: the owner read the
posting and said yes or no. This is the export that was missing between the
two.

## Why a swipe is `FEEDBACK` and not `OWNER`

`OWNER` means the owner sat down and graded a posting on the 0–3 scale.
`FEEDBACK` means a grade was inferred from something they did for another
reason. They are different evidence and the distinction is load-bearing:

- A swipe is **binary**, so it can only produce relevance 0 or 2. It cannot
  tell "would apply" from "would drop everything for", and NDCG's `2**rel`
  gain is precisely what that gap is for. A feedback-only set therefore
  measures ordering coarsely and cannot reward getting the top slot right.
- A swipe is **taken in feed order**, so it is only ever recorded for postings
  the ranker already surfaced. Nothing the ranker buried is ever labeled,
  which is the classic feedback loop: the model is graded on its own
  shortlist. `docs/ML_EVALUATION.md` is the place that has to keep saying so.

Neither makes the labels worthless — they are real judgements about real
postings, which is more than any fixture can claim. They just are not
interchangeable with graded ones, so they carry a different provenance and
the harness can tell them apart.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Match, Posting, Profile
from packages.matching.labels import LabeledPosting, LabeledSet, Provenance

#: A swipe is binary, so only these two grades are reachable. `2` rather than
#: `3` because "I would apply" is what the gesture means; reserving `3` keeps
#: the top of the scale for a grade the owner actually chose.
INTERESTED_RELEVANCE = 2
SKIPPED_RELEVANCE = 0

#: Below this the set cannot say anything about ranking — you need enough of
#: both classes for a metric to move. Reported rather than enforced: a caller
#: may legitimately want to look at three labels.
MIN_USEFUL_LABELS = 20


@dataclass(frozen=True)
class ExportReport:
    profile: str
    interested: int
    skipped: int
    undecided: int

    @property
    def labeled(self) -> int:
        return self.interested + self.skipped

    def summary(self) -> str:
        lines = [
            f"{self.profile}: {self.labeled} labeled "
            f"({self.interested} interested, {self.skipped} skipped), "
            f"{self.undecided} still undecided"
        ]
        if self.labeled < MIN_USEFUL_LABELS:
            lines.append(
                f"  {self.labeled} is below the {MIN_USEFUL_LABELS} a ranking metric "
                "needs to say anything. Keep swiping."
            )
        if not self.interested or not self.skipped:
            lines.append(
                "  One class only — every metric is degenerate until both "
                "'interested' and 'skipped' appear."
            )
        return "\n".join(lines)


def _key(posting: Posting) -> str:
    """A stable, readable id. The external id where there is one."""
    raw = posting.external_id or str(posting.id)
    return "fb-" + re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")[:48]


async def export_decisions(
    session: AsyncSession,
    profile: Profile,
    *,
    name: str = "owner-feedback",
    version: str = "v1",
) -> tuple[LabeledSet | None, ExportReport]:
    """Build a `LabeledSet` from every decided Match for this profile.

    Returns `None` for the set when nothing has been decided yet — an empty
    corpus is not a corpus, and `load_labeled_set` would refuse it anyway.
    """
    rows = (
        await session.execute(
            select(Match, Posting)
            .join(Posting, Posting.id == Match.posting_id)
            .where(Match.profile_id == profile.id)
        )
    ).all()

    items: list[LabeledPosting] = []
    interested = skipped = undecided = 0
    for match, posting in rows:
        if match.decision == "interested":
            relevance, interested = INTERESTED_RELEVANCE, interested + 1
        elif match.decision == "skipped":
            relevance, skipped = SKIPPED_RELEVANCE, skipped + 1
        else:
            undecided += 1
            continue

        items.append(
            LabeledPosting(
                key=_key(posting),
                title=posting.title or "",
                description=posting.description_raw or "",
                relevance=relevance,
                provenance=Provenance.FEEDBACK,
                company="",
                location=posting.location or "Remote",
                note=f"swiped {match.decision}",
                tags=("feedback",),
            )
        )

    report = ExportReport(
        profile=profile.label,
        interested=interested,
        skipped=skipped,
        undecided=undecided,
    )
    if not items:
        return None, report

    return (
        LabeledSet(
            name=name,
            version=version,
            profile_text=profile.label,
            items=tuple(items),
            description=(
                "Derived from swipe decisions in /swipe. Binary, and taken in "
                "feed order — see packages/matching/feedback.py for what that "
                "does and does not license."
            ),
        ),
        report,
    )
