"""The Projects section on the résumé an application actually sends.

`profile_text()` already counts GitHub projects when scoring, so a posting
whose skills live in the owner's repositories rather than their résumé ranks
*up* and is never filtered out — skills are not a hard filter. Measured on a
Platform Engineer posting against a résumé with no Kubernetes: 0.107 -> 0.245
lexical, 0.655 -> 0.734 semantic, with `missing_terms` going from four entries
to none.

The document did not agree. `_tailor` called `publish_tailored` without
`projects`, which defaults to None, so the PDF sent to the employer carried no
Projects section — the evidence that raised the score was absent from the page.
The comment above that call already said the résumé is rendered "with the
current Projects section rebuilt into it". It was describing an intention.
"""

from __future__ import annotations

import uuid

import pytest

from packages.core.models import Application, Candidate, Profile, Project, Resume, User
from packages.github.select import relevant_for_posting

JOB = (
    "Platform Engineer. You will run Kubernetes clusters, write Terraform "
    "modules and manage Helm releases. Docker experience required."
)


def _project(**kw) -> Project:
    defaults = dict(
        candidate_id=uuid.uuid4(),
        source="github",
        external_id=uuid.uuid4().hex[:8],
        name="repo",
        full_name="you/repo",
        url="https://github.com/you/repo",
        description="A repository.",
        language=None,
        topics_json=[],
        is_fork=False,
        is_archived=False,
        pinned=False,
        include=None,
        # Non-null in the schema with a server default of 0, so a real row
        # always has them. These objects are never flushed, and the default
        # only applies at INSERT — so they are set here rather than left None.
        stars=0,
        forks=0,
        homepage=None,
        pushed_at=None,
    )
    defaults.update(kw)
    return Project(**defaults)


def test_a_project_that_evidences_the_posting_is_kept() -> None:
    k8s = _project(
        name="k8s-homelab",
        description="Kubernetes homelab with Terraform and Helm charts",
        language="HCL",
        topics_json=["kubernetes", "terraform", "helm", "docker"],
    )
    assert relevant_for_posting([k8s], JOB) == [k8s]


def test_an_unrelated_project_does_not_consume_resume_space() -> None:
    """The reason this is not just `select_projects`.

    That function fills up to its limit by ranking, so with a thin inventory
    an irrelevant repository still lands on the page. On a résumé tailored to
    one posting, a project that evidences nothing about it is worse than a
    shorter section.
    """
    recipes = _project(
        name="recipe-book",
        description="A collection of pasta recipes",
        language="Markdown",
        topics_json=["cooking"],
    )
    assert relevant_for_posting([recipes], JOB) == []


def test_a_pinned_project_appears_whatever_the_posting_says() -> None:
    """`is_eligible` already promises a pinned project always makes the cut,
    and the relevance gate must not quietly break that promise."""
    pinned = _project(
        name="recipe-book",
        description="A collection of pasta recipes",
        topics_json=["cooking"],
        pinned=True,
    )
    assert relevant_for_posting([pinned], JOB) == [pinned]


def test_an_excluded_project_stays_excluded_even_when_it_matches() -> None:
    """`include=False` is the owner's decision and outranks relevance."""
    hidden = _project(
        name="k8s-homelab",
        description="Kubernetes homelab with Terraform and Helm",
        topics_json=["kubernetes", "terraform"],
        include=False,
    )
    assert relevant_for_posting([hidden], JOB) == []


def test_no_posting_text_yields_nothing_rather_than_everything() -> None:
    """A posting we cannot read is not a licence to attach the whole inventory.

    `relevance` returns 0.0 for empty job text, so without this the gate would
    reject everything anyway — pinned projects excepted. Pinned by design.
    """
    k8s = _project(name="k8s-homelab", description="Kubernetes and Terraform")
    pinned = _project(name="pinned", description="Always shown", pinned=True)

    assert relevant_for_posting([k8s, pinned], "") == [pinned]


@pytest.mark.asyncio
async def test_the_apply_pipeline_attaches_them(db_session, monkeypatch) -> None:
    """The regression that started this: the call site, not the helper.

    Asserts on what `publish_tailored` is handed, because that is the argument
    that was missing and a rendered PDF would not say which projects went in.
    """
    from apps.worker import apply_job

    user = User(email=f"o-{uuid.uuid4().hex[:8]}@example.com")
    db_session.add(user)
    await db_session.flush()
    candidate = Candidate(user_id=user.id, name="O", email=f"c-{uuid.uuid4().hex[:6]}@e.com")
    db_session.add(candidate)
    await db_session.flush()

    db_session.add(
        _project(
            candidate_id=candidate.id,
            name="k8s-homelab",
            description="Kubernetes homelab with Terraform and Helm charts",
            topics_json=["kubernetes", "terraform", "helm"],
        )
    )
    await db_session.flush()

    resume = Resume(
        candidate_id=candidate.id,
        version=1,
        storage_ref=f"resumes/{candidate.id}/v1/r.txt",
        parsed_json={
            "raw_lines": ["Experience", "Built backend services in Python."],
            # `_tailor` reads `section("experience")`; raw_lines alone leaves it
            # empty and the function returns before it ever publishes.
            "sections": {"experience": ["Built backend services in Python."]},
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
    await db_session.flush()

    handed: dict[str, object] = {}

    async def _capture(session, **kwargs):
        handed.update(kwargs)
        return None

    monkeypatch.setattr("packages.tailor.publish.publish_tailored", _capture)

    await apply_job._tailor(db_session, application, profile, JOB)

    assert "projects" in handed, "publish_tailored was called without `projects` again"
    assert [p.name for p in handed["projects"]] == ["k8s-homelab"]
