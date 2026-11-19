"""Kaggle requests an allowlisted MCP read before selecting fresh graph evidence."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from apps.api.github_tools import parse_plan
from packages.core.models import Match, Profile, Project
from packages.github.client import GitHubClient
from packages.llm.provider import LLMError, StubProvider
from packages.matching.requirements import extract
from tests.test_github_live import SHA, handler_factory
from tests.test_graph_rag import posting, seed

LEGACY_GAP_QUESTION = (
    "Compared with my résumés, which skills do my top matches ask for that I am missing?"
)


@pytest.mark.parametrize("fenced", [False, True])
@pytest.mark.parametrize(
    "payload",
    [
        {"tool_calls": [{"name": "delete_repository", "arguments": {"project_id": "known"}}]},
        {"tool_calls": [{"name": "github_project_evidence", "arguments": {"project_id": "other"}}]},
        {
            "tool_calls": [
                {
                    "name": "github_project_evidence",
                    "arguments": {"project_id": "known", "token": "secret"},
                }
            ]
        },
        {"tool_calls": []},
        {
            "tool_calls": [
                {"name": "github_project_evidence", "arguments": {"project_id": "known"}}
            ]
            * 2
        },
    ],
)
def test_tool_requests_are_validated_before_any_execution(payload, fenced):
    raw = json.dumps(payload)
    if fenced:
        raw = f"```json\n{raw}\n```"
    with pytest.raises(LLMError):
        parse_plan(raw, {"known"})


@pytest.mark.parametrize(
    "wrapper", ["{}", "```json\n{}\n```", "```JSON\r\n{}\r\n```", "```\n{}\n```"]
)
def test_valid_tool_plan_accepts_one_complete_markdown_json_wrapper(wrapper):
    raw = json.dumps(
        {"tool_calls": [{"name": "github_project_evidence", "arguments": {"project_id": "known"}}]}
    )
    plan = parse_plan(" \n" + wrapper.format(raw) + "\n ", {"known"})
    assert plan.tool_calls[0].arguments.project_id == "known"


@pytest.mark.parametrize(
    "wrapper",
    [
        "Here is the plan:\n```json\n{}\n```",
        "```json\n{}\n```\nExtra prose",
        "```json\n{}\n```\n```json\n{{}}\n```",
        "```python\n{}\n```",
    ],
)
def test_wrapped_tool_plan_does_not_extract_json_from_prose_or_multiple_blocks(wrapper):
    raw = json.dumps(
        {"tool_calls": [{"name": "github_project_evidence", "arguments": {"project_id": "known"}}]}
    )
    with pytest.raises(LLMError):
        parse_plan(wrapper.format(raw), {"known"})


@pytest.mark.parametrize("path", ["/chat", "/chat/stream"])
@pytest.mark.parametrize("fenced", [False, True])
@pytest.mark.parametrize(
    "question", ["Which jobs match skills from my projects?", LEGACY_GAP_QUESTION]
)
async def test_kaggle_uses_real_mcp_protocol_and_current_github_evidence(
    path,
    fenced,
    question,
    client,
    worker_session,
    monkeypatch,
):
    from apps.api.routers import chat

    job = await seed(worker_session)
    if question == LEGACY_GAP_QUESTION:
        job.description_raw = (
            "Requirements:\nPython and FastAPI are required.\nPreferred:\nTerraform."
        )
        job.requirements_json = extract(job.description_raw).as_json()
        other_job = posting(job.description_raw, company_id=job.company_id)
        worker_session.add(other_job)
        await worker_session.flush()
        profile_id = await worker_session.scalar(select(Profile.id))
        worker_session.add(Match(profile_id=profile_id, posting_id=other_job.id, score=0.8))
    repo = await worker_session.scalar(select(Project))
    repo.full_name, repo.external_id, repo.language = "owner/example", "17", "Rust"
    repo.dependency_technologies = []
    await worker_session.commit()
    handler, requests = handler_factory()
    real_init = GitHubClient.__init__

    def patched(self, token=None, **kwargs):
        import httpx

        real_init(self, token, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(GitHubClient, "__init__", patched)
    prompts = []
    factory_calls = []

    class Kaggle(StubProvider):
        model = "pinned-kaggle-model"

        async def complete(self, system, user, **kwargs):
            prompts.append(user)
            if len(prompts) == 1:
                assert "source-project" not in user and "owner/example" not in user
                raw = json.dumps(
                    {
                        "tool_calls": [
                            {
                                "name": "github_project_evidence",
                                "arguments": {"project_id": str(repo.id)},
                            }
                        ]
                    }
                )
                return f"```json\n{raw}\n```" if fenced else raw
            assert ("FastAPI" if question == LEGACY_GAP_QUESTION else "Python") in user
            assert "Rust" not in user
            assert SHA in user and "checked" in user
            return '{"ids": ["P1"]}'

    provider = Kaggle()

    def factory(selected):
        factory_calls.append(selected)
        return provider, provider.model

    monkeypatch.setattr(chat, "provider_for", factory)
    response = await client.post(
        path,
        json={
            "message": question,
            "provider": "kaggle",
        },
    )
    assert response.status_code == 200, response.text
    result = (
        json.loads(response.text.splitlines()[-1])["reply"]
        if path.endswith("stream")
        else response.json()
    )
    assert len(prompts) == 2 and factory_calls == ["kaggle"]
    assert len(requests) == 4
    assert result["provider"] == "kaggle" and result["local"] is False
    assert result["model"] == provider.model
    assert result["sources"][0]["cited"]
    if question == LEGACY_GAP_QUESTION:
        assert "FastAPI" in result["reply"] and "2 of 2" in result["reply"]
        assert "project also declares" in result["reply"]
        assert "Terraform" not in result["reply"]
    else:
        assert f"/tree/{SHA}" in result["retrieval_details"]
    assert "https://" not in result["reply"]
    assert "Source text" not in result["reply"] and "```" not in result["reply"]
    assert "Repository declaration" not in result["reply"]
    assert "Live GitHub MCP" not in result["reply"]
    assert "Live GitHub MCP" in result["retrieval_details"]
    assert SHA in result["retrieval_details"]
    assert result["postings_scope"] == "top_matches"
    assert result["retrieval_method"] == "graph"
    await worker_session.refresh(repo)
    assert repo.language == "Rust", "live graph snapshots must not rewrite the project cache"


async def test_failed_live_tools_do_not_fall_back_to_stale_projects_or_claim_local_answer(
    client,
    worker_session,
    monkeypatch,
):
    import apps.mcp.github as tools
    from apps.api.routers import chat

    await seed(worker_session)
    repo = await worker_session.scalar(select(Project))
    repo.full_name = "owner/example"
    await worker_session.commit()

    async def unavailable(*args, **kwargs):
        from packages.github.client import GitHubError

        raise GitHubError("offline")

    monkeypatch.setattr(tools, "read_live_project", unavailable)
    calls = []

    class Kaggle(StubProvider):
        model = "pinned-kaggle-model"

        async def complete(self, system, user, **kwargs):
            calls.append(user)
            return json.dumps(
                {
                    "tool_calls": [
                        {
                            "name": "github_project_evidence",
                            "arguments": {"project_id": str(repo.id)},
                        }
                    ]
                }
            )

    monkeypatch.setattr(chat, "provider_for", lambda selected: (Kaggle(), "pinned-kaggle-model"))
    response = await client.post(
        "/chat",
        json={
            "message": "Which jobs match skills from my projects?",
            "provider": "kaggle",
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(calls) == 1 and not result["sources"]
    assert "1 checks failed" in result["reply"]
    assert result["provider"] == "kaggle" and result["local"] is False
