"""The labeling loop — docs/BACKLOG.md P1.

Every relevance label in this repo is a `FIXTURE`, which is why
`docs/ML_EVALUATION.md` refuses to name a production candidate and why
CLAUDE.md §15 says Gate 5 does not answer the question it was written to ask.

The tests that matter here are not "a grade round-trips". They are the ones
that keep the corpus from acquiring `provenance: owner` while carrying the
sampling bias that provenance is supposed to mean it escaped.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Match, Posting, PostingLabel
from packages.matching import active
from packages.matching.labels import Provenance


@pytest.fixture
async def corpus(client: AsyncClient, worker_session: AsyncSession, complete_candidate) -> dict:
    """Ten scored postings across the score range, plus five never scored.

    The unscored five are the point: they have no `Match` row, so they can
    never be swiped, and before this loop they could never be labeled either.
    """
    profile_id = uuid.UUID(complete_candidate["profile_id"])

    scored: list[Posting] = []
    for i in range(10):
        posting = Posting(
            url=f"https://boards.greenhouse.io/acme/jobs/s{i}",
            title=f"Backend Engineer {i}",
            description_raw="Python, PostgreSQL, Kubernetes.",
        )
        worker_session.add(posting)
        scored.append(posting)

    unscored: list[Posting] = []
    for i in range(5):
        posting = Posting(
            url=f"https://boards.greenhouse.io/acme/jobs/u{i}",
            title=f"Filtered Out Role {i}",
            description_raw="A posting no hard filter let through.",
        )
        worker_session.add(posting)
        unscored.append(posting)
    await worker_session.flush()

    # Scores spread 0.05..0.50 so there is a real midpoint and a real top.
    for i, posting in enumerate(scored):
        worker_session.add(
            Match(profile_id=profile_id, posting_id=posting.id, score=0.05 + i * 0.05)
        )
    await worker_session.commit()

    return {
        "profile_id": str(profile_id),
        "scored": [str(p.id) for p in scored],
        "unscored": [str(p.id) for p in unscored],
    }


# --------------------------------------------------------------------------
# Selection — the half that separates these labels from swipe-derived ones
# --------------------------------------------------------------------------


async def test_the_queue_serves_postings_the_ranker_never_scored(
    client: AsyncClient, corpus
) -> None:
    """The whole reason this is not `/swipe` with more buttons.

    `feedback.py` records that a swipe is only ever taken on postings the
    ranker surfaced, so the model is graded on its own shortlist. A posting
    dropped by a hard filter has no Match row and can never be swiped. If it
    cannot be graded here either, `provenance: owner` would claim an escape
    from that bias that had not happened.
    """
    served = (await client.get("/labels/next", params={"size": 10})).json()

    streams = {c["stream"] for c in served}
    assert active.Stream.UNSEEN.value in streams

    unseen = [c for c in served if c["stream"] == active.Stream.UNSEEN.value]
    assert all(c["posting_id"] in corpus["unscored"] for c in unseen)
    assert all(c["score"] is None for c in unseen)


async def test_a_score_of_none_is_not_reported_as_zero(client: AsyncClient, corpus) -> None:
    """Null means the scorer never had an opinion. Zero means it looked and
    said no. Collapsing them would make the unseen stream unreadable — the one
    stream whose entire value is recording that the ranker did not weigh in."""
    served = (await client.get("/labels/next", params={"size": 10})).json()

    unseen = [c for c in served if c["stream"] == active.Stream.UNSEEN.value]
    assert unseen
    assert all(c["score"] is None for c in unseen)


async def test_the_queue_mixes_streams_rather_than_serving_one(client: AsyncClient, corpus) -> None:
    """Uncertainty sampling alone cannot show that the ranker is confidently
    wrong about a whole category — by construction it only serves postings the
    ranker has an opinion on."""
    served = (await client.get("/labels/next", params={"size": 10})).json()

    assert len({c["stream"] for c in served}) >= 2


async def test_a_graded_posting_is_not_served_again(client: AsyncClient, corpus) -> None:
    """The queue has to drain, or the loop never reaches 100 labels."""
    first = (await client.get("/labels/next", params={"size": 5})).json()
    target = first[0]["posting_id"]

    await client.post("/labels", json={"posting_id": target, "relevance": 2})

    again = (await client.get("/labels/next", params={"size": 10})).json()
    assert target not in {c["posting_id"] for c in again}


async def test_a_short_stream_does_not_backfill_from_the_others(
    client: AsyncClient, worker_session: AsyncSession, complete_candidate
) -> None:
    """On a database with nothing unscored, the unseen quota goes unfilled
    rather than being handed to `uncertain`. Backfilling would quietly return
    an all-shortlist batch, which is this module's bias arriving by the back
    door."""
    profile_id = uuid.UUID(complete_candidate["profile_id"])
    posting = Posting(url="https://boards.greenhouse.io/acme/jobs/only", title="Only Role")
    worker_session.add(posting)
    await worker_session.flush()
    worker_session.add(Match(profile_id=profile_id, posting_id=posting.id, score=0.4))
    await worker_session.commit()

    served = (await client.get("/labels/next", params={"size": 10})).json()

    assert active.Stream.UNSEEN.value not in {c["stream"] for c in served}
    assert len(served) == 1


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------


async def test_a_grade_is_recorded_with_the_score_the_ranker_gave(
    client: AsyncClient, worker_session: AsyncSession, corpus
) -> None:
    """Stored, not looked up later: re-scoring moves the number, and "what did
    the ranker think when a human disagreed" is the measurement."""
    posting_id = corpus["scored"][0]

    body = (await client.post("/labels", json={"posting_id": posting_id, "relevance": 3})).json()
    assert body["relevance"] == 3

    stored = await worker_session.scalar(
        select(PostingLabel).where(PostingLabel.posting_id == uuid.UUID(posting_id))
    )
    assert stored is not None
    assert stored.score_at_label == pytest.approx(0.05)
    assert stored.stream is not None


async def test_re_grading_overwrites_rather_than_duplicating(
    client: AsyncClient, worker_session: AsyncSession, corpus
) -> None:
    """A corpus you cannot correct is one you build carefully and slowly."""
    posting_id = corpus["scored"][1]

    await client.post("/labels", json={"posting_id": posting_id, "relevance": 0})
    await client.post("/labels", json={"posting_id": posting_id, "relevance": 3, "note": "reread"})

    rows = (
        await worker_session.scalars(
            select(PostingLabel).where(PostingLabel.posting_id == uuid.UUID(posting_id))
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].relevance == 3
    assert rows[0].note == "reread"


@pytest.mark.parametrize("bad", [-1, 4, 99])
async def test_a_grade_outside_the_scale_is_refused(client: AsyncClient, corpus, bad: int) -> None:
    """`2**rel` gain in NDCG breaks silently on an out-of-range grade rather
    than raising, so it must not be storable."""
    response = await client.post(
        "/labels", json={"posting_id": corpus["scored"][0], "relevance": bad}
    )
    # 400 rather than 422: the app flattens pydantic's structure into the
    # shared `invalid_request` envelope (§10) rather than forking the contract.
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


async def test_grading_applies_to_nothing(
    client: AsyncClient, worker_session: AsyncSession, corpus
) -> None:
    """§2.3: nothing submits without explicit approval, and a grade is not
    that. The same line `/swipe` draws."""
    from packages.core.models import Application

    await client.post("/labels", json={"posting_id": corpus["scored"][0], "relevance": 3})

    assert (await worker_session.scalars(select(Application))).all() == []


# --------------------------------------------------------------------------
# The summary, which has to report coverage and not only count
# --------------------------------------------------------------------------


async def test_the_summary_warns_when_no_unseen_posting_has_been_graded(
    client: AsyncClient, corpus
) -> None:
    """A hundred labels drawn entirely from the ranker's shortlist is the
    failure this loop exists to avoid, and a total on its own cannot show it."""
    await client.post("/labels", json={"posting_id": corpus["scored"][0], "relevance": 2})

    summary = (await client.get("/labels/summary")).json()

    assert summary["total"] == 1
    assert any("unseen stream" in note for note in summary["notes"])


async def test_the_summary_reports_the_stream_mix(client: AsyncClient, corpus) -> None:
    """The mix is reported beside the count because they answer different
    questions, and only one of them can show a shortlist-only corpus."""
    await client.post("/labels", json={"posting_id": corpus["scored"][0], "relevance": 1})
    await client.post("/labels", json={"posting_id": corpus["unscored"][0], "relevance": 0})

    summary = (await client.get("/labels/summary")).json()

    assert summary["total"] == 2
    assert summary["by_stream"].get(active.Stream.UNSEEN.value) == 1
    assert sum(summary["by_grade"].values()) == 2


async def test_one_grade_only_is_called_degenerate(client: AsyncClient, corpus) -> None:
    """Every ranking metric is degenerate until the corpus disagrees with
    itself somewhere."""
    for posting_id in corpus["scored"][:3]:
        await client.post("/labels", json={"posting_id": posting_id, "relevance": 2})

    summary = (await client.get("/labels/summary")).json()

    assert not summary["usable"]
    assert any("One grade only" in note for note in summary["notes"])


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------


async def test_grades_export_as_owner_not_feedback(
    client: AsyncClient, worker_session: AsyncSession, corpus
) -> None:
    """The distinction the whole loop exists to earn. `FEEDBACK` is a grade
    inferred from a swipe; `OWNER` is one the owner chose on the 0-3 scale."""
    from packages.core.models import Profile
    from packages.matching.owner_labels import export_owner_labels

    await client.post("/labels", json={"posting_id": corpus["scored"][0], "relevance": 3})
    await client.post("/labels", json={"posting_id": corpus["unscored"][0], "relevance": 0})

    profile = await worker_session.get(Profile, uuid.UUID(corpus["profile_id"]))
    assert profile is not None
    labeled, report = await export_owner_labels(worker_session, profile)

    assert labeled is not None
    assert len(labeled.items) == 2
    assert all(item.provenance is Provenance.OWNER for item in labeled.items)
    assert not labeled.is_fixture_only
    assert report.total == 2


async def test_an_empty_corpus_exports_nothing_rather_than_an_empty_set(
    worker_session: AsyncSession, complete_candidate
) -> None:
    """`load_labeled_set` refuses an empty corpus, so producing one would only
    move the failure somewhere less obvious."""
    from packages.core.models import Profile
    from packages.matching.owner_labels import export_owner_labels

    profile = await worker_session.get(Profile, uuid.UUID(complete_candidate["profile_id"]))
    assert profile is not None

    labeled, report = await export_owner_labels(worker_session, profile)

    assert labeled is None
    assert report.total == 0


# --------------------------------------------------------------------------
# Review findings on #76, each reproduced before it was fixed
# --------------------------------------------------------------------------


@pytest.mark.parametrize("size", [1, 2, 3, 4, 5, 7, 10, 20, 50])
def test_the_quota_always_sums_to_the_batch_size(size: int) -> None:
    """Rounding each share independently over-allocated on every size that is
    not a multiple of the mix, and the caller truncated the tail. `size=4` gave
    `2+2+1` and lost `confident`; `size=1` gave `1+1+1` and left uncertain
    alone — the shortlist bias arriving through an arithmetic bug."""
    quota = active._quota(size)

    assert sum(quota.values()) == size
    assert all(count >= 0 for count in quota.values())


def test_every_stream_gets_a_slot_once_the_batch_can_hold_them() -> None:
    """Below three there are not enough slots and the shares decide. At three
    and above, a batch that silently carried only one stream would be the
    failure this module exists to prevent."""
    for size in range(len(active.STREAM_MIX), 30):
        quota = active._quota(size)
        assert all(count >= 1 for count in quota.values()), size


async def test_the_stored_stream_matches_the_stream_that_served_it(
    client: AsyncClient, worker_session: AsyncSession, complete_candidate
) -> None:
    """With one scored posting the range is flat, so `_uncertain` returns
    nothing and `_confident` is what served the card. The recorded stream said
    `uncertain` — and the recorded one is the audit trail, so it was the copy
    that lied."""
    profile_id = uuid.UUID(complete_candidate["profile_id"])
    posting = Posting(url="https://boards.greenhouse.io/acme/jobs/flat", title="Only Role")
    worker_session.add(posting)
    await worker_session.flush()
    worker_session.add(Match(profile_id=profile_id, posting_id=posting.id, score=0.4))
    await worker_session.commit()

    served = (await client.get("/labels/next", params={"size": 10})).json()
    await client.post("/labels", json={"posting_id": served[0]["posting_id"], "relevance": 2})

    stored = await worker_session.scalar(
        select(PostingLabel).where(PostingLabel.posting_id == posting.id)
    )
    assert stored is not None
    assert stored.stream == served[0]["stream"]


async def test_two_grades_racing_the_same_posting_both_succeed(
    client: AsyncClient, worker_session: AsyncSession, corpus
) -> None:
    """Read-then-insert let two writes both see no row, and the unique
    constraint then failed one — so a double-tap returned an error instead of
    a grade. Key auto-repeat makes that easy to hit."""
    import asyncio

    posting_id = corpus["scored"][0]
    first, second = await asyncio.gather(
        client.post("/labels", json={"posting_id": posting_id, "relevance": 1}),
        client.post("/labels", json={"posting_id": posting_id, "relevance": 3}),
    )

    assert first.status_code == 200
    assert second.status_code == 200

    rows = (
        await worker_session.scalars(
            select(PostingLabel).where(PostingLabel.posting_id == uuid.UUID(posting_id))
        )
    ).all()
    assert len(rows) == 1


async def test_a_re_grade_does_not_restate_what_the_ranker_thought(
    client: AsyncClient, worker_session: AsyncSession, corpus
) -> None:
    """`score_at_label` and `stream` describe the moment of the *first* grade.
    Re-scoring moves the score, and letting a re-grade overwrite them would
    quietly rewrite the history the measurement rests on."""
    posting_id = corpus["scored"][0]
    await client.post("/labels", json={"posting_id": posting_id, "relevance": 1})

    stored = await worker_session.scalar(
        select(PostingLabel).where(PostingLabel.posting_id == uuid.UUID(posting_id))
    )
    assert stored is not None
    original_score, original_stream = stored.score_at_label, stored.stream

    await client.post("/labels", json={"posting_id": posting_id, "relevance": 3})
    await worker_session.refresh(stored)

    assert stored.relevance == 3
    assert stored.score_at_label == original_score
    assert stored.stream == original_stream


async def test_a_corpus_with_no_unseen_label_is_never_called_usable(
    client: AsyncClient, corpus
) -> None:
    """The predicate, not just a note beside it. A hundred labels with two
    grades and no `unseen` is the corpus this loop was built to avoid, and
    calling it usable would send the owner to export it."""
    for i, posting_id in enumerate(corpus["scored"]):
        await client.post("/labels", json={"posting_id": posting_id, "relevance": i % 2})

    summary = (await client.get("/labels/summary")).json()

    assert summary["total"] == len(corpus["scored"])
    assert not summary["by_stream"].get(active.Stream.UNSEEN.value)
    assert not summary["usable"]


async def test_the_export_report_names_the_stream_mix(
    client: AsyncClient, worker_session: AsyncSession, corpus
) -> None:
    """`make export-labels` prints this, and a count of labels cannot show
    whether the corpus escaped the shortlist."""
    from packages.core.models import Profile
    from packages.matching.owner_labels import export_owner_labels

    await client.post("/labels", json={"posting_id": corpus["scored"][0], "relevance": 2})

    profile = await worker_session.get(Profile, uuid.UUID(corpus["profile_id"]))
    assert profile is not None
    _, report = await export_owner_labels(worker_session, profile)

    assert "streams:" in report.summary()
    assert "unseen stream" in report.summary()


async def test_a_withdrawn_score_is_not_counted_as_unseen(
    client: AsyncClient, worker_session: AsyncSession, corpus
) -> None:
    """`score.score_and_store` deletes a stale `Match` row when a posting newly
    fails a hard filter. So a card served as `uncertain` can reach the grade
    with its row already gone, and the recompute would see no score and record
    `unseen` — inflating the one number that certifies the corpus escaped the
    shortlist bias. Reproduced before the fix: served `uncertain`, stored
    `unseen`.

    `unknown` is the honest answer: not proven unseen, so not counted as it.
    """
    served = (await client.get("/labels/next", params={"size": 10})).json()
    target = next(c for c in served if c["stream"] != active.Stream.UNSEEN.value)

    stale = await worker_session.scalar(
        select(Match).where(Match.posting_id == uuid.UUID(target["posting_id"]))
    )
    await worker_session.delete(stale)
    await worker_session.commit()

    await client.post(
        "/labels",
        json={
            "posting_id": target["posting_id"],
            "relevance": 2,
            "served_stream": target["stream"],
        },
    )

    stored = await worker_session.scalar(
        select(PostingLabel).where(PostingLabel.posting_id == uuid.UUID(target["posting_id"]))
    )
    assert stored is not None
    assert stored.stream == active.Stream.UNKNOWN.value

    summary = (await client.get("/labels/summary")).json()
    assert not summary["by_stream"].get(active.Stream.UNSEEN.value)


async def test_the_hint_can_never_be_used_to_claim_unseen(
    client: AsyncClient, worker_session: AsyncSession, corpus
) -> None:
    """`served_stream` is one-way. A client claiming `unseen` for a posting the
    ranker did score gets the server's answer, not its own — otherwise the hint
    would be a way to manufacture the very coverage it exists to protect."""
    posting_id = corpus["scored"][0]

    await client.post(
        "/labels",
        json={"posting_id": posting_id, "relevance": 2, "served_stream": "unseen"},
    )

    stored = await worker_session.scalar(
        select(PostingLabel).where(PostingLabel.posting_id == uuid.UUID(posting_id))
    )
    assert stored is not None
    assert stored.stream != active.Stream.UNSEEN.value


async def test_a_genuinely_unseen_posting_is_still_recorded_as_unseen(
    client: AsyncClient, worker_session: AsyncSession, corpus
) -> None:
    """The fail-safe must not cost the real thing. A posting the ranker never
    scored, graded with the hint the screen actually showed, still counts."""
    posting_id = corpus["unscored"][0]

    await client.post(
        "/labels",
        json={"posting_id": posting_id, "relevance": 3, "served_stream": "unseen"},
    )

    stored = await worker_session.scalar(
        select(PostingLabel).where(PostingLabel.posting_id == uuid.UUID(posting_id))
    )
    assert stored is not None
    assert stored.stream == active.Stream.UNSEEN.value
