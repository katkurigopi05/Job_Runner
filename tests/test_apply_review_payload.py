"""What `_tailor` actually hands the review screen.

`_tailor` does not return its `DiffSummary`. It hand-builds a dict, naming
each field, and *that* dict becomes `review_json["resume_diff"]`. So a field
can be computed, carried on the model, typed on the client and rendered by a
component, and still never arrive — the screen shows nothing and reports no
error, because there is no error.

That is what happened to `ats`: `summarize` computed it, `DiffSummary` held
it, `ResumeDiffView` had a panel for it, and the dict below did not list it.
It is CLAUDE.md §15's defect in miniature — a feature that runs in full and is
invisible — and the reason the assertions here are on the payload rather than
on the model.
"""

from __future__ import annotations

import uuid

import pytest

from packages.core.models import Application, Candidate, Posting, Profile, Resume, User

JOB = """
Senior Backend Engineer. We run Python services on Kubernetes with PostgreSQL.
Kubernetes, PostgreSQL and Python throughout. Kubernetes. PostgreSQL. Python.
You will build data pipelines and own services end to end.
"""


async def _fixture(db_session) -> tuple[Application, Profile, Posting]:
    user = User(email=f"o-{uuid.uuid4().hex[:8]}@example.com")
    db_session.add(user)
    await db_session.flush()

    candidate = Candidate(user_id=user.id, name="O", email=f"c-{uuid.uuid4().hex[:6]}@e.com")
    db_session.add(candidate)
    await db_session.flush()

    resume = Resume(
        candidate_id=candidate.id,
        version=1,
        storage_ref=f"resumes/{candidate.id}/v1/r.txt",
        parsed_json={
            "raw_lines": [
                "Experience",
                "Built backend services in Python and Postgres.",
                "Wrote the deployment tooling for Kubernetes.",
            ],
            "sections": {
                "experience": [
                    "Built backend services in Python and Postgres.",
                    "Wrote the deployment tooling for Kubernetes.",
                ]
            },
        },
    )
    db_session.add(resume)
    await db_session.flush()

    profile = Profile(candidate_id=candidate.id, label="p", base_resume_id=resume.id)
    db_session.add(profile)
    await db_session.flush()

    application = Application(
        candidate_id=candidate.id,
        profile_id=profile.id,
        url=f"https://x.test/{uuid.uuid4().hex[:8]}",
        ats="greenhouse",
    )
    db_session.add(application)

    posting = Posting(url=f"https://x.test/p/{uuid.uuid4().hex[:8]}", description_raw=JOB)
    db_session.add(posting)
    await db_session.flush()

    return application, profile, posting


@pytest.mark.asyncio
async def test_the_ats_score_reaches_the_review_payload(db_session, monkeypatch) -> None:
    """The bug this file exists for: computed, carried, and never serialized."""
    from apps.worker import apply_job

    application, profile, posting = await _fixture(db_session)

    async def _capture(session, **kwargs):
        return None

    monkeypatch.setattr("packages.tailor.publish.publish_tailored", _capture)

    payload = await apply_job._tailor(db_session, application, profile, posting)

    assert payload is not None
    assert "ats" in payload, "the review screen's ATS panel gets nothing"
    ats = payload["ats"]
    assert ats is not None

    for key in (
        "parse_before",
        "parse_after",
        "keywords_before",
        "keywords_after",
        "gained",
        "still_missing",
    ):
        assert key in ats, key


@pytest.mark.asyncio
async def test_the_payload_is_json_serializable(db_session, monkeypatch) -> None:
    """It is stored in a JSON column, so a model instance in it would raise."""
    import json

    from apps.worker import apply_job

    application, profile, posting = await _fixture(db_session)

    async def _capture(session, **kwargs):
        return None

    monkeypatch.setattr("packages.tailor.publish.publish_tailored", _capture)

    payload = await apply_job._tailor(db_session, application, profile, posting)
    assert payload is not None
    json.dumps(payload)


@pytest.mark.asyncio
async def test_a_posting_with_no_text_leaves_the_ats_field_absent(db_session, monkeypatch) -> None:
    """Absent, not zero. A 0% score and "not measured" are different claims."""
    from apps.worker import apply_job

    application, profile, posting = await _fixture(db_session)
    posting.description_raw = ""
    await db_session.flush()

    async def _capture(session, **kwargs):
        return None

    monkeypatch.setattr("packages.tailor.publish.publish_tailored", _capture)

    payload = await apply_job._tailor(db_session, application, profile, posting)
    if payload is not None:
        assert payload.get("ats") is None


@pytest.mark.asyncio
async def test_the_recruiter_score_reaches_the_review_payload(db_session, monkeypatch) -> None:
    """The second field down the same path, asserted for the same reason.

    `summarize` computes it, `DiffSummary` carries it, `ResumeDiffView` has a
    panel for it — and none of that matters unless `_tailor`'s hand-built dict
    names it. This file exists because that is exactly how `ats` was lost.
    """
    from apps.worker import apply_job

    application, profile, posting = await _fixture(db_session)

    async def _capture(session, **kwargs):
        return None

    monkeypatch.setattr("packages.tailor.publish.publish_tailored", _capture)

    payload = await apply_job._tailor(db_session, application, profile, posting)

    assert payload is not None
    assert "recruiter" in payload, "the review screen's recruiter panel gets nothing"
    recruiter = payload["recruiter"]
    assert recruiter is not None

    for key in (
        "before",
        "after",
        "shortlist_before",
        "shortlist_after",
        "scan_after",
        "qualification_after",
        "credibility_after",
        "technical_after",
        "findings",
    ):
        assert key in recruiter, f"the panel reads {key} and the payload does not carry it"

    assert 0.0 <= recruiter["after"] <= 1.0
    assert recruiter["shortlist_after"], "a score with no verdict is not actionable"


@pytest.mark.asyncio
async def test_the_two_scores_are_carried_apart(db_session, monkeypatch) -> None:
    """Never merged. They answer different questions and can disagree, and the
    disagreement is the reason the second one exists."""
    from apps.worker import apply_job

    application, profile, posting = await _fixture(db_session)

    async def _capture(session, **kwargs):
        return None

    monkeypatch.setattr("packages.tailor.publish.publish_tailored", _capture)

    payload = await apply_job._tailor(db_session, application, profile, posting)

    assert payload is not None
    assert payload["ats"] is not None
    assert payload["recruiter"] is not None
    assert payload["ats"].keys() != payload["recruiter"].keys()
