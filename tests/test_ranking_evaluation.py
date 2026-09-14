"""Held-out evaluation refuses to validate what the data cannot support."""

from __future__ import annotations

import uuid

from packages.core.models import Match, Posting, PostingLabel
from packages.matching import evaluation
from packages.matching.evaluation import LEARNED_MODEL, evaluate, is_held_out


class _Pref:
    def __init__(self, kind: str, value: str, weight: float) -> None:
        self.kind, self.value, self.weight = kind, value, weight


async def _profile_id(db_session, application) -> uuid.UUID:
    return application.profile_id


async def test_zero_owner_labels_is_insufficient_and_claims_no_model(
    db_session, application
) -> None:
    report = await evaluate(db_session, application.profile_id, [_Pref("title_term", "x", 0.1)])

    assert report.status == "insufficient_labels"
    assert report.promotable is False
    assert report.learned_model == LEARNED_MODEL
    assert any("0 owner grades" in blocker for blocker in report.blockers)
    assert report.message.startswith("Not validated.")


def test_the_held_out_split_is_stable() -> None:
    posting = uuid.uuid4()

    assert is_held_out(posting) == is_held_out(posting)


async def _grade(db_session, profile_id, *, count: int, relevant_titles: set[str]) -> None:
    for index in range(count):
        title = f"Role {index} {'python' if index % 2 else 'java'}"
        posting = Posting(url=f"https://eval.test/{index}", title=title)
        db_session.add(posting)
        await db_session.flush()
        relevance = 3 if "python" in title else 0
        # The base score gets it backwards by a margin a +0.30 preference can
        # overturn — so only the preference can fix the order.
        score = 0.45 if "python" in title else 0.55
        db_session.add(Match(profile_id=profile_id, posting_id=posting.id, score=score))
        db_session.add(
            PostingLabel(
                profile_id=profile_id,
                posting_id=posting.id,
                relevance=relevance,
                stream="uncertain" if index % 3 else "unseen",
            )
        )
    await db_session.flush()


async def test_enough_grades_evaluate_and_a_clear_improvement_is_promotable(
    db_session, application, monkeypatch
) -> None:
    monkeypatch.setattr(evaluation, "MIN_OWNER_LABELS", 40)
    monkeypatch.setattr(evaluation, "MIN_HELD_OUT", 5)
    await _grade(db_session, application.profile_id, count=160, relevant_titles={"python"})

    report = await evaluate(
        db_session, application.profile_id, [_Pref("title_term", "python", 0.3)]
    )

    assert report.status == "evaluated", report.blockers
    assert report.personalized_ndcg > report.base_ndcg
    assert report.promotable is True


async def test_a_preference_that_does_not_help_is_not_promotable(
    db_session, application, monkeypatch
) -> None:
    monkeypatch.setattr(evaluation, "MIN_OWNER_LABELS", 40)
    monkeypatch.setattr(evaluation, "MIN_HELD_OUT", 5)
    await _grade(db_session, application.profile_id, count=160, relevant_titles={"python"})

    report = await evaluate(db_session, application.profile_id, [_Pref("title_term", "java", 0.05)])

    assert report.promotable is False
    assert report.message.startswith("Evaluated: no demonstrated improvement")


async def test_the_endpoint_reports_the_verdict_for_the_only_profile(
    client, complete_candidate
) -> None:
    response = await client.get("/ranking/evaluation")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "insufficient_labels"
    assert body["promotable"] is False
    assert body["learned_model"].startswith("none")
