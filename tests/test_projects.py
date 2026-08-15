"""GitHub project ingestion, selection, and résumé rendering.

The load-bearing test here is `test_default_style_survives_text_extraction`.
A link icon alone puts the URL only in the PDF's link annotation, which an ATS
that reads the text layer never sees — so the default style is verified to be
parseable, not assumed to be.

No test touches the network. GitHub responses come from a recorded fixture.
"""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pypdf import PdfReader

from packages.core.models import Project
from packages.github.client import GitHubClient, GitHubError, RateLimited, Repository
from packages.github.select import (
    is_eligible,
    recency,
    relevance,
    score,
    select_projects,
    substance,
)
from packages.tailor.projects import (
    LINK_ICON,
    LinkStyle,
    ProjectEntry,
    compact_slug,
    render_section_html,
    render_section_pdf,
)

NOW = datetime(2026, 8, 15, tzinfo=UTC)


def repo_payload(**overrides) -> dict:
    """One repository as the GitHub API actually shapes it."""
    payload = {
        "id": 123456,
        "name": "jobrunner",
        "full_name": "octocat/jobrunner",
        "html_url": "https://github.com/octocat/jobrunner",
        "homepage": "https://jobrunner.dev",
        "description": "Local job-application agent with a Postgres queue",
        "language": "Python",
        "topics": ["automation", "playwright", "postgres"],
        "stargazers_count": 42,
        "forks_count": 3,
        "fork": False,
        "archived": False,
        "private": False,
        "pushed_at": "2026-08-01T10:00:00Z",
    }
    payload.update(overrides)
    return payload


def make_project(**overrides) -> Project:
    defaults = dict(
        id=uuid.uuid4(),
        candidate_id=uuid.uuid4(),
        source="github",
        external_id=str(uuid.uuid4()),
        name="jobrunner",
        full_name="octocat/jobrunner",
        url="https://github.com/octocat/jobrunner",
        homepage=None,
        description="Local job-application agent",
        language="Python",
        topics_json=["automation"],
        stars=10,
        forks=0,
        is_fork=False,
        is_archived=False,
        is_private=False,
        pushed_at=NOW - timedelta(days=10),
        include=None,
        pinned=False,
    )
    defaults.update(overrides)
    return Project(**defaults)


# --------------------------------------------------------------------------
# Client — against a mock transport, never the network
# --------------------------------------------------------------------------


def _transport(pages: list[list[dict]], status: int = 200, headers: dict | None = None):
    """Serve recorded pages, keyed on the page parameter GitHub is sent."""

    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, headers=headers or {}, json={"message": "nope"})
        page = int(request.url.params.get("page", 1))
        body = pages[page - 1] if 0 < page <= len(pages) else []
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


async def test_lists_repositories() -> None:
    client = GitHubClient(transport=_transport([[repo_payload()]]))
    repos = await client.list_repositories("octocat")

    assert len(repos) == 1
    assert repos[0].name == "jobrunner"
    assert repos[0].url == "https://github.com/octocat/jobrunner"
    assert repos[0].topics == ["automation", "playwright", "postgres"]


async def test_missing_description_stays_missing() -> None:
    """§2.1 — an empty description is never filled in with a guess."""
    client = GitHubClient(transport=_transport([[repo_payload(description=None)]]))
    repos = await client.list_repositories("octocat")
    assert repos[0].description is None


async def test_rate_limit_is_reported_clearly() -> None:
    client = GitHubClient(
        transport=_transport([], status=403, headers={"x-ratelimit-remaining": "0"})
    )
    with pytest.raises(RateLimited, match="GITHUB_TOKEN"):
        await client.list_repositories("octocat")


async def test_not_found_is_an_error() -> None:
    client = GitHubClient(transport=_transport([], status=404))
    with pytest.raises(GitHubError, match="not found"):
        await client.list_repositories("nobody")


async def test_private_listing_requires_a_token() -> None:
    client = GitHubClient(transport=_transport([[]]))
    with pytest.raises(GitHubError, match="requires a GITHUB_TOKEN"):
        await client.list_repositories("octocat", include_private=True)


def test_repository_parses_api_shape() -> None:
    repo = Repository.from_api(repo_payload(private=True, fork=True, archived=True))
    assert repo.is_private and repo.is_fork and repo.is_archived
    assert repo.pushed_at is not None


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------


def test_forks_and_archived_are_ineligible() -> None:
    assert not is_eligible(make_project(is_fork=True))
    assert not is_eligible(make_project(is_archived=True))


def test_undescribed_project_is_ineligible_by_default() -> None:
    """The alternatives are a bare name or an invented summary; §2.1 bars both."""
    assert not is_eligible(make_project(description=None))
    assert is_eligible(make_project(description=None), require_description=False)


def test_owner_choice_overrides_the_rules() -> None:
    assert is_eligible(make_project(is_fork=True, include=True))
    assert not is_eligible(make_project(include=False))


def test_relevance_matches_posting_language() -> None:
    project = make_project(description="Async Python service", language="Python")
    assert relevance(project, "We need a Python engineer") > 0
    assert relevance(project, "Looking for a pastry chef") == 0.0


def test_relevance_without_a_posting_is_zero() -> None:
    assert relevance(make_project(), "") == 0.0


def test_recency_decays() -> None:
    fresh = make_project(pushed_at=NOW - timedelta(days=1))
    old = make_project(pushed_at=NOW - timedelta(days=900))
    ancient = make_project(pushed_at=NOW - timedelta(days=5000))

    assert recency(fresh, now=NOW) > recency(old, now=NOW) > 0
    assert recency(ancient, now=NOW) == 0.0
    assert recency(make_project(pushed_at=None), now=NOW) == 0.0


def test_stars_are_a_weak_signal() -> None:
    """A well-matched project must beat a popular unrelated one."""
    relevant = make_project(
        name="python-queue", description="Postgres job queue in Python", stars=1
    )
    popular = make_project(name="dotfiles", description="My shell config", stars=5000)
    job = "Python engineer, Postgres, job queues, backend systems"

    assert score(relevant, job, now=NOW) > score(popular, job, now=NOW)


def test_substance_rewards_completeness() -> None:
    bare = make_project(description="x", topics_json=[], homepage=None, stars=0)
    full = make_project(description="x", topics_json=["a"], homepage="https://x.dev", stars=50)
    assert substance(full) > substance(bare)


def test_pinned_projects_always_make_the_cut() -> None:
    """The owner can guarantee a project appears on every résumé."""
    pinned = make_project(name="pinned-one", pushed_at=NOW - timedelta(days=1000), pinned=True)
    others = [make_project(name=f"recent-{i}", pushed_at=NOW - timedelta(days=i)) for i in range(6)]

    chosen = select_projects([*others, pinned], "", limit=3, now=NOW)

    assert chosen[0].name == "pinned-one"
    assert len(chosen) == 3


def test_selection_respects_the_limit() -> None:
    projects = [make_project(name=f"p{i}") for i in range(10)]
    assert len(select_projects(projects, "", limit=4, now=NOW)) == 4


def test_selection_excludes_ineligible() -> None:
    projects = [make_project(name="ok"), make_project(name="forked", is_fork=True)]
    names = {p.name for p in select_projects(projects, "", now=NOW)}
    assert names == {"ok"}


def test_selection_is_deterministic() -> None:
    """Same inputs, same résumé — every time."""
    projects = [make_project(name=f"p{i}", stars=i) for i in range(8)]
    first = [p.name for p in select_projects(projects, "python", now=NOW)]
    second = [p.name for p in select_projects(projects, "python", now=NOW)]
    assert first == second


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def test_compact_slug() -> None:
    assert compact_slug("https://github.com/you/repo") == "github.com/you/repo"
    assert compact_slug("https://www.github.com/you/repo/") == "github.com/you/repo"


def test_html_contains_the_real_href() -> None:
    html = render_section_html([make_project()])
    assert 'href="https://github.com/octocat/jobrunner"' in html
    assert LINK_ICON in html


def test_html_escapes_project_text() -> None:
    """Repo descriptions are third-party text; they must not inject markup."""
    project = make_project(description='<script>alert("x")</script>')
    html = render_section_html([project])
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_empty_section_is_omitted() -> None:
    assert render_section_html([]) == ""


def test_description_is_never_invented() -> None:
    html = render_section_html([make_project(description=None)], style=LinkStyle.ICON_SLUG)
    assert "jobrunner" in html
    assert "project-desc" not in html


# --------------------------------------------------------------------------
# The ATS round-trip — the reason link style is configurable
# --------------------------------------------------------------------------


def _extract(pdf_bytes: bytes) -> tuple[str, list[str]]:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    page = reader.pages[0]
    text = page.extract_text() or ""
    urls = []
    for annot in page.get("/Annots") or []:
        action = annot.get_object().get("/A")
        if action and action.get("/URI"):
            urls.append(str(action["/URI"]))
    return text, urls


def test_default_style_survives_text_extraction() -> None:
    """The default must leave a usable link in the layer an ATS reads."""
    pdf = render_section_pdf([make_project()], style=LinkStyle.ICON_SLUG)
    text, urls = _extract(pdf)

    assert "github.com/octocat/jobrunner" in text, (
        "the default link style must be visible to a text-extracting parser"
    )
    assert "https://github.com/octocat/jobrunner" in urls


def test_icon_only_loses_the_url_in_the_text_layer() -> None:
    """Documents the tradeoff rather than leaving it to be discovered later."""
    pdf = render_section_pdf([make_project()], style=LinkStyle.ICON_ONLY)
    text, urls = _extract(pdf)

    assert "github.com" not in text
    # Still clickable for a human — the link is in the annotation.
    assert "https://github.com/octocat/jobrunner" in urls


def test_full_url_style_is_parseable() -> None:
    pdf = render_section_pdf([make_project()], style=LinkStyle.FULL_URL)
    text, urls = _extract(pdf)
    assert "https://github.com/octocat/jobrunner" in text.replace("\n", "")
    assert urls


def test_pdf_carries_the_project_name_and_description() -> None:
    pdf = render_section_pdf([make_project()])
    text, _ = _extract(pdf)
    assert "jobrunner" in text
    assert "Local job-application agent" in text


def test_every_selected_project_is_linked() -> None:
    projects = [
        make_project(name="alpha", url="https://github.com/octocat/alpha"),
        make_project(name="beta", url="https://github.com/octocat/beta"),
    ]
    _, urls = _extract(render_section_pdf(projects))
    assert set(urls) == {
        "https://github.com/octocat/alpha",
        "https://github.com/octocat/beta",
    }


def test_entry_from_project_is_source_faithful() -> None:
    project = make_project()
    entry = ProjectEntry.from_project(project)
    assert entry.name == project.name
    assert entry.description == project.description
    assert entry.url == project.url
