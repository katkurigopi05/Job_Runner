"""Does the personalized order rank better than the base one, on held-out grades?

`docs/BACKLOG.md` P3's finish line: "the personalized variant beats
`production` on held-out owner-labeled data with non-overlapping intervals.
If it doesn't, that is a result — report it and stop."

This is that check, and its most common answer today is **not enough data**.
It reads the owner's own 0–3 grades from `/label`, holds out a fixed share of
them, and computes NDCG@k for the base score and for the personalized score
over the held-out postings, each with a bootstrap interval. It will not call
anything promotable unless the personalized interval sits wholly above the
base one *and* there are enough owner labels, from more than one sampling
stream, for that to mean something.

## What this never claims

- **There is no learned model.** Personalization is the owner's explicit
  adjustments. `learned_model` is always reported as none; a trained reranker
  would be evaluated here too, and would have to clear the same bar.
- **Swipes are not held-out grades.** They are binary and taken in feed
  order (`feedback.py`), so they are excluded — a ranker graded on its own
  shortlist cannot show it ranks.
- **Explicit preferences are not "validated" by being liked.** They apply
  because the owner set them; this says whether they also rank better.
"""

from __future__ import annotations

import hashlib
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Company, Match, Posting, PostingLabel
from packages.matching.metrics import Judgement, bootstrap_ci, ndcg_at_k
from packages.matching.personalize import PreferenceLike, adjust

#: P1's "done when": at least this many owner grades before a ranking change
#: may be called validated.
MIN_OWNER_LABELS = 100
#: And they must come from more than one `active.Stream`, or the corpus is the
#: ranker's own shortlist again.
MIN_STREAMS = 2
#: Share of labeled postings held out, chosen by a stable hash of the posting
#: id so the split never changes between runs and never leaks.
HOLDOUT_PERCENT = 30
#: Fewer held-out postings than this and an interval is noise.
MIN_HELD_OUT = 20

LEARNED_MODEL = "none — personalization is explicit, owner-set adjustments only"


def is_held_out(posting_id: uuid.UUID) -> bool:
    digest = hashlib.sha256(str(posting_id).encode()).digest()
    return digest[0] * 100 // 256 < HOLDOUT_PERCENT


@dataclass
class EvaluationReport:
    owner_labels: int
    streams: dict[str, int]
    held_out: int
    preferences: int
    status: str
    message: str
    k: int
    learned_model: str = LEARNED_MODEL
    base_ndcg: float | None = None
    base_interval: tuple[float, float] | None = None
    personalized_ndcg: float | None = None
    personalized_interval: tuple[float, float] | None = None
    promotable: bool = False
    blockers: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "owner_labels": self.owner_labels,
            "streams": self.streams,
            "held_out": self.held_out,
            "preferences": self.preferences,
            "status": self.status,
            "message": self.message,
            "k": self.k,
            "learned_model": self.learned_model,
            "base_ndcg": self.base_ndcg,
            "base_interval": list(self.base_interval) if self.base_interval else None,
            "personalized_ndcg": self.personalized_ndcg,
            "personalized_interval": (
                list(self.personalized_interval) if self.personalized_interval else None
            ),
            "promotable": self.promotable,
            "blockers": self.blockers,
        }


async def evaluate(
    session: AsyncSession,
    profile_id: uuid.UUID,
    preferences: Sequence[PreferenceLike],
    *,
    k: int = 10,
) -> EvaluationReport:
    rows = (
        await session.execute(
            select(PostingLabel, Posting, Match.score, Company.name)
            .join(Posting, Posting.id == PostingLabel.posting_id)
            .outerjoin(
                Match,
                (Match.posting_id == PostingLabel.posting_id)
                & (Match.profile_id == PostingLabel.profile_id),
            )
            .outerjoin(Company, Company.id == Posting.company_id)
            .where(PostingLabel.profile_id == profile_id)
        )
    ).all()

    streams = Counter(label.stream or "unrecorded" for label, _, _, _ in rows)
    held = [row for row in rows if is_held_out(row[1].id)]
    report = EvaluationReport(
        owner_labels=len(rows),
        streams=dict(streams),
        held_out=len(held),
        preferences=len(preferences),
        status="insufficient_labels",
        message="",
        k=k,
    )

    if len(rows) < MIN_OWNER_LABELS:
        report.blockers.append(
            f"{len(rows)} owner grades; {MIN_OWNER_LABELS} needed (grade postings on /label)"
        )
    if len([s for s in streams if s != "unrecorded"]) < MIN_STREAMS:
        report.blockers.append(
            f"grades come from {len(streams)} sampling stream(s); {MIN_STREAMS} needed, or the "
            "set is the ranker's own shortlist"
        )
    if len(held) < MIN_HELD_OUT:
        report.blockers.append(
            f"{len(held)} held-out grades; {MIN_HELD_OUT} needed for an interval"
        )
    if not preferences:
        report.blockers.append("no ranking preferences set, so there is nothing to compare")

    if held and preferences:
        base: list[Judgement] = []
        personal: list[Judgement] = []
        for label, posting, score, company in held:
            # A posting the ranker never scored is ranked last, not guessed at.
            starting = score if score is not None else (label.score_at_label or 0.0)
            base.append(Judgement(starting, label.relevance, str(posting.id)))
            personal.append(
                Judgement(
                    adjust(starting, posting, company, preferences)[0],
                    label.relevance,
                    str(posting.id),
                )
            )
        report.base_ndcg = ndcg_at_k(base, k)
        report.personalized_ndcg = ndcg_at_k(personal, k)
        report.base_interval = bootstrap_ci(base, lambda items: ndcg_at_k(items, k))  # type: ignore[arg-type]
        report.personalized_interval = bootstrap_ci(
            personal,
            lambda items: ndcg_at_k(items, k),  # type: ignore[arg-type]
        )
        separated = report.personalized_interval[0] > report.base_interval[1]
        if not report.blockers:
            report.status = "evaluated"
            report.promotable = separated
            if not separated:
                report.blockers.append(
                    "intervals overlap: the personalized order is not shown to rank better"
                )

    if report.status == "evaluated" and report.promotable:
        report.message = "The personalized order ranks better on held-out grades, beyond the noise."
    elif report.status == "evaluated":
        report.message = "Evaluated: no demonstrated improvement. Keep the base order as default."
    else:
        report.message = (
            "Not validated. Explicit preferences still apply when you choose the personalized "
            "order, but nothing here shows they rank better: " + "; ".join(report.blockers) + "."
        )
    return report


__all__ = [
    "LEARNED_MODEL",
    "MIN_HELD_OUT",
    "MIN_OWNER_LABELS",
    "EvaluationReport",
    "evaluate",
    "is_held_out",
]
