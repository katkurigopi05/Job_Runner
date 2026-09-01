"""Two models, the same posting, and a choice between them.

§7 made the provider settable per task. What it could not answer is the
question that decides the setting — *is the cloud one better for my résumé and
this job?* — because answering it meant editing `.env`, re-running, and holding
the first result in your head.

Three properties are load-bearing and easy to undo:

- **Both sides are guard-checked before either is shown.** A comparison offers
  each column as something the owner may choose and send. An unvetted draft
  presented that way is a fabricated bullet with a button under it.
- **A side that cannot run is reported, not dropped.** A comparison silently
  missing half of itself reads as a verdict.
- **Selecting is restricted to the two that were offered.** This sets the file
  that reaches an employer.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from packages.core.models import Candidate, Posting, Profile, Resume, User
from packages.tailor import compare as compare_mod
from packages.tailor.compare import Candidate as Side
from packages.tailor.compare import CannotCompare, compare_tailorings

JOB = "Backend engineer. Python services on Postgres, Kubernetes, and on-call."


async def _profile(db_session, *, bullets: list[str] | None = None) -> tuple[Profile, Resume]:
    suffix = uuid.uuid4().hex[:8]
    user = User(email=f"o-{suffix}@example.com")
    db_session.add(user)
    await db_session.flush()
    candidate = Candidate(user_id=user.id, name="Owner", email=f"c-{suffix}@example.com")
    db_session.add(candidate)
    await db_session.flush()

    lines = ["Built backend services in Python."] if bullets is None else bullets
    resume = Resume(
        candidate_id=candidate.id,
        version=1,
        storage_ref=f"resumes/{candidate.id}/v1/r.txt",
        parsed_json={"raw_lines": lines, "sections": {"experience": lines}},
    )
    db_session.add(resume)
    await db_session.flush()

    profile = Profile(candidate_id=candidate.id, label="p", base_resume_id=resume.id)
    db_session.add(profile)
    await db_session.flush()
    return profile, resume


async def _posting(db_session, *, text: str = JOB) -> Posting:
    posting = Posting(url=f"https://x.test/p/{uuid.uuid4().hex[:8]}", description_raw=text)
    db_session.add(posting)
    await db_session.flush()
    return posting


def _sides_are(monkeypatch, results: dict[str, Side]) -> list[str]:
    """Replace the per-side work, and record the order it was asked for."""
    asked: list[str] = []

    async def fake(session, *, provider_name, **kwargs):  # noqa: ANN001
        asked.append(provider_name)
        return results[provider_name]

    monkeypatch.setattr(compare_mod, "tailor_with", fake)
    return asked


@pytest.mark.asyncio
async def test_it_compares_the_local_model_against_the_cloud_one(db_session, monkeypatch) -> None:
    profile, _ = await _profile(db_session)
    posting = await _posting(db_session)

    monkeypatch.setattr(compare_mod, "cloud_for_tailoring", lambda: "gemini")
    asked = _sides_are(
        monkeypatch,
        {
            "ollama": Side(requested="ollama", answered_by="ollama:llama3.1", changed=2),
            "gemini": Side(requested="gemini", answered_by="gemini", changed=3),
        },
    )

    candidates = await compare_tailorings(db_session, profile=profile, posting=posting)

    # Local first: it costs nothing and cannot fail on quota, so when the remote
    # half is refused the owner still has a document rather than an empty screen.
    assert asked == ["ollama", "gemini"]
    assert [c.answered_by for c in candidates] == ["ollama:llama3.1", "gemini"]


@pytest.mark.asyncio
async def test_no_cloud_provider_is_a_reported_side_not_a_missing_one(
    db_session, monkeypatch
) -> None:
    """One column with no explanation reads as a verdict on the other."""
    profile, _ = await _profile(db_session)
    posting = await _posting(db_session)

    monkeypatch.setattr(compare_mod, "cloud_for_tailoring", lambda: None)
    _sides_are(monkeypatch, {"ollama": Side(requested="ollama", answered_by="ollama:llama3.1")})

    candidates = await compare_tailorings(db_session, profile=profile, posting=posting)

    assert len(candidates) == 2
    assert candidates[1].error is not None
    assert candidates[1].resume_id is None
    assert "no remote provider is configured" in candidates[1].error


@pytest.mark.asyncio
async def test_a_failing_side_does_not_take_the_other_down(db_session, monkeypatch) -> None:
    """A spent allowance is a normal outcome here, not a crash."""
    from packages.llm.quota import QuotaExceeded

    profile, resume = await _profile(db_session)
    posting = await _posting(db_session)

    monkeypatch.setattr(compare_mod, "cloud_for_tailoring", lambda: "gemini")

    async def fake_build(name):  # noqa: ANN001
        raise AssertionError("should not be reached")

    async def half_broken(session, *, provider_name, **kwargs):  # noqa: ANN001
        if provider_name == "gemini":
            return compare_mod.Candidate(
                requested="gemini", error=str(QuotaExceeded("gemini", 204, 200))
            )
        return Side(requested="ollama", answered_by="ollama:llama3.1", resume_id=resume.id)

    monkeypatch.setattr(compare_mod, "tailor_with", half_broken)

    candidates = await compare_tailorings(db_session, profile=profile, posting=posting)

    assert candidates[0].resume_id == resume.id
    assert candidates[1].error is not None
    assert "204 of 200" in candidates[1].error


@pytest.mark.asyncio
async def test_a_cache_hit_sends_nothing_and_says_so(db_session, monkeypatch) -> None:
    """§2.8's cheapest upload is the one not made."""
    profile, base = await _profile(db_session)
    posting = await _posting(db_session)
    posting.content_hash = "abc123"
    await db_session.flush()

    already = Resume(
        candidate_id=base.candidate_id,
        version=2,
        storage_ref="resumes/x/v2/tailored.pdf",
        parsed_json={},
        tailored_by="gemini",
    )
    db_session.add(already)
    await db_session.flush()

    called = False

    async def never(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("a cache hit must not call a provider")

    monkeypatch.setattr(compare_mod, "cloud_for_tailoring", lambda: None)
    monkeypatch.setattr(compare_mod, "tailor_bullets", never)
    monkeypatch.setattr(compare_mod, "build_provider", lambda name: _StubProvider())
    monkeypatch.setattr(compare_mod, "find_cached", _returns(already))

    candidates = await compare_tailorings(db_session, profile=profile, posting=posting)

    assert called is False
    assert candidates[0].reused is True
    assert candidates[0].resume_id == already.id
    assert candidates[0].answered_by == "gemini"


class _StubProvider:
    name = "ollama"
    model = "llama3.1"


def _returns(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner


@pytest.mark.asyncio
async def test_nothing_to_compare_is_refused_rather_than_shown_as_two_failures(
    db_session,
) -> None:
    """A precondition is not a model outcome and must not be dressed up as one."""
    profile, _ = await _profile(db_session)
    posting = await _posting(db_session, text="   ")

    with pytest.raises(CannotCompare, match="no description"):
        await compare_tailorings(db_session, profile=profile, posting=posting)


# --------------------------------------------------------------------------
# The routes
# --------------------------------------------------------------------------

APPLY_URL = "https://boards.greenhouse.io/acme/jobs/9911"


async def test_comparing_a_queued_application_is_invalid_state(
    client: AsyncClient, complete_candidate
) -> None:
    """Re-tailoring something not parked uploads a résumé to no purpose."""
    created = await client.post("/applications", json={**complete_candidate, "url": APPLY_URL})

    r = await client.post(f"/applications/{created.json()['id']}/tailoring/compare")

    assert r.status_code == 409
    assert r.json()["error"]["code"] == "invalid_state"


async def test_choosing_a_resume_that_was_not_offered_is_refused(
    client: AsyncClient, complete_candidate
) -> None:
    """This sets the file an employer receives; the screen offers exactly two."""
    created = await client.post("/applications", json={**complete_candidate, "url": APPLY_URL})

    r = await client.post(
        f"/applications/{created.json()['id']}/tailoring/select",
        json={"resume_id": str(uuid.uuid4())},
    )

    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


# --------------------------------------------------------------------------
# Naming the remote half
# --------------------------------------------------------------------------
#
# The default is whatever real tailoring would use, which answers the usual
# question. It could not answer it for OpenRouter: §7 keeps that out of
# `QUALITY_ORDER`, so the only way to compare against it was
# `LLM_TASK_TAILOR=openrouter` — which also redirects every real tailoring call.
# The owner had to adopt a provider in order to evaluate it, which is the exact
# friction this comparison was built to remove.


@pytest.mark.asyncio
async def test_a_named_cloud_is_the_side_that_runs(db_session, monkeypatch) -> None:
    """The point of the parameter."""
    monkeypatch.setattr(compare_mod, "is_comparable_cloud", lambda name: True)
    profile, _ = await _profile(db_session)
    posting = await _posting(db_session)
    asked = _sides_are(
        monkeypatch,
        {
            "ollama": Side(requested="ollama", changed=1),
            "openrouter": Side(requested="openrouter", changed=2),
        },
    )

    await compare_tailorings(db_session, profile=profile, posting=posting, cloud="openrouter")

    assert asked == ["ollama", "openrouter"]


@pytest.mark.asyncio
async def test_omitting_it_still_uses_what_tailoring_would(db_session, monkeypatch) -> None:
    """The shipped default has to stay the shipped default."""
    monkeypatch.setattr(compare_mod, "cloud_for_tailoring", lambda: "gemini")
    profile, _ = await _profile(db_session)
    posting = await _posting(db_session)
    asked = _sides_are(
        monkeypatch,
        {
            "ollama": Side(requested="ollama", changed=1),
            "gemini": Side(requested="gemini", changed=2),
        },
    )

    await compare_tailorings(db_session, profile=profile, posting=posting)

    assert asked == ["ollama", "gemini"]


@pytest.mark.asyncio
async def test_an_unconfigured_cloud_is_refused_not_shown_as_a_failed_column(
    db_session, monkeypatch
) -> None:
    """A precondition, not a model outcome.

    The owner asked for a specific comparison and did not get it. A column
    reading "openrouter: unavailable" beside a local one would look like a
    verdict on OpenRouter rather than on this machine's configuration.
    """
    monkeypatch.setattr(compare_mod, "is_comparable_cloud", lambda name: False)
    monkeypatch.setattr(compare_mod, "comparable_clouds", lambda: ["gemini"])
    profile, _ = await _profile(db_session)
    posting = await _posting(db_session)

    with pytest.raises(CannotCompare) as caught:
        await compare_tailorings(db_session, profile=profile, posting=posting, cloud="openrouter")

    assert "openrouter" in str(caught.value)
    # Names what would work, so the message is actionable.
    assert "gemini" in str(caught.value)


@pytest.mark.asyncio
async def test_the_local_model_cannot_be_named_as_the_cloud_half(db_session) -> None:
    """A model compared against itself is not a comparison."""
    from packages.llm.router import is_comparable_cloud

    assert is_comparable_cloud("ollama") is False
    assert is_comparable_cloud("stub") is False


def test_openrouter_is_comparable_but_still_not_automatic() -> None:
    """The §7 boundary this feature must not quietly erase.

    A comparison may be pointed at OpenRouter by name. Nothing may route to it
    by default — a key in `.env` still changes nothing on its own.
    """
    from packages.llm.router import COMPARABLE_CLOUD, QUALITY_ORDER

    assert "openrouter" in COMPARABLE_CLOUD
    assert "openrouter" not in QUALITY_ORDER


# --------------------------------------------------------------------------
# A provider that never answered is not a guard refusal
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_provider_failure_is_not_counted_as_a_guard_refusal() -> None:
    """Found on a real local-vs-ox-alpha run, where it misreported the column.

    OpenRouter returned an error, `tailor_bullet` kept the original line with
    `rejected_reason="provider error: LLMError"`, and the comparison displayed
    "1 refused by the guard" — which reads as "this model kept trying to
    invent". It never spoke at all. On a screen whose entire purpose is judging
    two models against each other, that is the wrong verdict on the wrong
    subject.
    """
    from packages.llm.provider import LLMError
    from packages.tailor.diff import summarize
    from packages.tailor.guard import SourceCorpus
    from packages.tailor.rewrite import tailor_bullets

    class Dead:
        name = "openrouter"

        async def complete(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise LLMError("upstream refused")

    bullets = ["Built the billing service in Python."]
    result = await tailor_bullets(
        Dead(), bullets, "Backend engineer.", SourceCorpus.from_texts(*bullets)
    )

    assert result.provider_failures == 1
    assert result.rejected == 0

    summary = summarize(result)
    assert summary.provider_failures == 1
    assert summary.rejected == 0
    # The original line is still kept — an honest untailored bullet beats none.
    assert result.tailored_lines == bullets


@pytest.mark.asyncio
async def test_a_guard_refusal_is_still_counted_as_one() -> None:
    """The other half. Splitting the counts must not empty the one that mattered."""
    from packages.tailor.diff import summarize
    from packages.tailor.guard import SourceCorpus
    from packages.tailor.rewrite import tailor_bullets

    class Inventing:
        name = "ollama"

        async def complete(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return "Led platform engineering at Netflix, cutting latency by 40%."

    bullets = ["Built the billing service in Python."]
    result = await tailor_bullets(
        Inventing(), bullets, "Backend engineer.", SourceCorpus.from_texts(*bullets)
    )

    assert result.rejected == 1
    assert result.provider_failures == 0
    assert summarize(result).rejected == 1
