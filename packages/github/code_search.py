"""Targeted GitHub source retrieval for résumé evidence."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import uuid
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel

from packages.core.config import get_settings
from packages.core.models import Project
from packages.github.client import GitHubClient, GitHubError, RateLimited
from packages.github.code_signals import Classification, Signal, inspect_file
from packages.github.infrastructure import is_infrastructure_path
from packages.github.live import _SHA, eligible, resolve_snapshot
from packages.github.manifests import is_manifest_path, is_project_path
from packages.github.select import project_skills
from packages.matching.requirements import Requirement, SkillMention
from packages.matching.skill_vocab import CODE_SKILLS, find_skills

REPO_LIMIT = 4
FILE_LIMIT = 10
FILE_BYTES = 64 * 1024
TREE_BYTES = 8 * 1024 * 1024
EXTRACTOR_VERSION = 1
CACHE_LIMIT = 64
SIGNALS_PER_FILE = 128

# Search text comes from these rules, never interpolated from the JD or source.
_SEARCH = {
    "aws_lambda": "lambda_handler",
    "fastapi": "APIRouter",
    "rest": "APIRouter",
    "aws": "boto3",
    "kubernetes": "containers",
    "docker": "FROM",
    "cicd": "steps",
    "github_actions": "steps",
    "python": "def",
}
_PYTHON_SKILLS = {"python", "fastapi", "rest", "aws", "aws_lambda"}
_RELATED = {
    "aws_lambda": {"aws", "python"},
    "aws": {"aws_lambda"},
    "rest": {"fastapi", "flask", "django"},
    "cicd": {"github_actions", "jenkins"},
    "github_actions": {"cicd"},
}
_IGNORED = {
    "tests",
    "test",
    "examples",
    "example",
    "samples",
    "sample",
    "docs",
    "mocks",
    "tutorials",
    "templates",
    "third-party",
    "generated",
    "external",
    "vendors",
    "demos",
    "demo",
}


class CodeFinding(BaseModel):
    id: str
    skills: tuple[str, ...]
    project_id: uuid.UUID
    repository_id: str
    repository_name: str
    full_name: str
    is_private: bool
    path: str
    start_line: int
    end_line: int
    commit_sha: str
    permalink: str | None
    explanation: str
    classification: Classification
    verification_status: str = "commit verified"
    can_support_claim: bool
    wording: tuple[str, ...] = ()


@dataclass
class SearchResult:
    findings: list[CodeFinding] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    searched: int = 0

    def implemented(self) -> dict[uuid.UUID, set[str]]:
        verified: dict[uuid.UUID, set[str]] = {}
        for finding in self.findings:
            if finding.can_support_claim:
                verified.setdefault(finding.project_id, set()).update(finding.skills)
        return verified


# Public derived signals only. Identity and latest commit are resolved before
# this cache is consulted. No source text, authorization data, or failures.
_cache: OrderedDict[tuple[str, str, int, str, str], tuple[Signal, ...]] = OrderedDict()


def _candidates(
    projects: Sequence[Project], needs: Sequence[SkillMention], *, allow_private: bool
) -> list[Project]:
    ranked = []
    for project in projects:
        if not eligible(project, allow_private=allow_private) or project.is_archived:
            continue
        declared = project_skills(project)
        metadata = (
            " ".join([project.name, project.description or "", *(project.topics_json or [])])
            .replace("-", " ")
            .replace("_", " ")
        )
        named = {skill.key for skill in find_skills(metadata, CODE_SKILLS)}
        relevance = sum(
            (3 if need.requirement is Requirement.REQUIRED and not need.option else 1)
            * (3 if need.skill in declared else 2 if need.skill in named else 1)
            for need in needs
            if need.skill in declared | named
            or _RELATED.get(need.skill, set()) & (declared | named)
        )
        if relevance:
            ranked.append((relevance, bool(project.resume_entry), bool(project.pinned), project))
    ranked.sort(key=lambda item: (-item[0], not item[1], not item[2], str(item[3].id)))
    unique: dict[str, Project] = {}
    for _, _, _, project in ranked:
        unique.setdefault(project.external_id, project)
    return list(unique.values())


async def _json(
    client: httpx.AsyncClient, github: GitHubClient, url: str, limit: int, **params: str
) -> dict[str, Any]:
    async with client.stream("GET", url, params=params) as response:
        github._raise_for_status(response)
        data = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=16384):
            if len(data) + len(chunk) > limit:
                raise GitHubError("GitHub response exceeded the evidence size limit.")
            data.extend(chunk)
    payload = json.loads(data)
    if not isinstance(payload, dict):
        raise GitHubError("GitHub returned an unreadable evidence response.")
    return payload


def _path_priority(path: str, skills: set[str], hints: set[str]) -> tuple[int, int, str] | None:
    parts = path.lower().split("/")
    name = parts[-1]
    if (
        any(part in {".", "..", ""} for part in parts)
        or not is_project_path(path)
        or set(parts[:-1]) & _IGNORED
        or name.startswith(("test_", "conftest."))
        or name.endswith("_test.py")
    ):
        return None
    if is_infrastructure_path(path):
        workflow = path.startswith(".github/workflows/")
        wanted = {"cicd", "github_actions"} if workflow else {"docker"}
        priority = 0 if wanted & skills else 5
    elif path.endswith(".py") and skills & _PYTHON_SKILLS:
        targeted_python = bool(
            ("aws_lambda" in skills and any(word in path.lower() for word in ("lambda", "handler")))
            or ("aws" in skills and any(word in name for word in ("aws", "s3", "dynamo")))
            or (
                skills & {"fastapi", "rest"}
                and any(word in path.lower() for word in ("routes", "router", "api"))
            )
        )
        priority = 1 if path in hints or targeted_python else 4
    elif path.endswith((".yml", ".yaml")) and skills & {"kubernetes", "aws", "aws_lambda"}:
        relevant_name = re.search(
            r"(?:k8s|kube|deploy|manifests?|cloudformation|serverless|template|lambda)", path, re.I
        )
        if not relevant_name and path not in hints:
            return None
        priority = 1
    elif is_manifest_path(path):
        priority = 3
    elif name in {"readme.md", "readme.rst", "readme.txt"} and len(parts) == 1:
        priority = 2
    else:
        return None
    return priority, len(parts), path


async def _signals(
    client: httpx.AsyncClient,
    github: GitHubClient,
    project: Project,
    base: str,
    commit: str,
    path: str,
    blob: str,
) -> tuple[Signal, ...]:
    key = project.external_id, commit, EXTRACTOR_VERSION, path, blob
    if not project.is_private and key in _cache:
        _cache.move_to_end(key)
        return _cache[key]

    data = await _json(client, github, f"{base}/git/blobs/{blob}", 2 * FILE_BYTES)
    if data.get("sha") != blob or data.get("encoding") != "base64":
        raise GitHubError("GitHub returned a mismatched source blob.")
    raw = base64.b64decode("".join(data["content"].split()), validate=True)
    header = f"blob {len(raw)}\0".encode()
    digest = hashlib.sha1 if len(blob) == 40 else hashlib.sha256
    if len(raw) > FILE_BYTES or data.get("size") != len(raw):
        raise GitHubError("GitHub source blob exceeded the file limit or changed size.")
    if digest(header + raw).hexdigest() != blob:
        raise GitHubError("GitHub source blob could not be verified.")

    unique: dict[tuple[tuple[str, ...], Classification, str], Signal] = {}
    for signal in inspect_file(path, raw.decode("utf-8")):
        unique.setdefault((signal.skills, signal.classification, signal.explanation), signal)
        if len(unique) >= SIGNALS_PER_FILE:
            break
    signals = tuple(unique.values())
    if not project.is_private:
        _cache[key] = signals
        if len(_cache) > CACHE_LIMIT:
            _cache.popitem(last=False)
    return signals


async def _scan(
    client: httpx.AsyncClient,
    github: GitHubClient,
    project: Project,
    needs: Sequence[SkillMention],
    result: SearchResult,
) -> None:
    repository, commit, tree_sha = await resolve_snapshot(client, github, project)
    if tree_sha is None or not re.fullmatch(_SHA, tree_sha):
        raise GitHubError("GitHub did not return a verifiable tree for the commit.")
    base = f"{github.api_root}/repos/{repository.full_name}"
    tree = await _json(client, github, f"{base}/git/trees/{tree_sha}", TREE_BYTES, recursive="1")
    if tree.get("sha") != tree_sha or not isinstance(tree.get("tree"), list):
        raise GitHubError("GitHub returned an unreadable repository tree.")
    result.searched += 1
    if tree.get("truncated"):
        result.limitations.append("GitHub truncated a repository tree.")

    skills = {need.skill for need in needs}
    query = next((_SEARCH[need.skill] for need in needs if need.skill in _SEARCH), None)
    hints: set[str] = set()
    if query and github.token:
        try:
            search = await _json(
                client,
                github,
                f"{github.api_root}/search/code",
                128 * 1024,
                q=f"{query} in:file repo:{repository.full_name}",
                per_page="8",
            )
            hints = {
                item["path"]
                for item in search["items"]
                if item["repository"]["full_name"].casefold() == repository.full_name.casefold()
                and isinstance(item.get("path"), str)
            }
            if search.get("incomplete_results"):
                result.limitations.append("GitHub returned incomplete code-search results.")
        except RateLimited:
            raise
        except (GitHubError, httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            result.limitations.append("GitHub code search was unavailable. Used the pinned tree.")
    elif query:
        result.limitations.append(
            "GitHub indexed code search requires a token. Used the pinned tree."
        )

    files: dict[str, tuple[tuple[int, int, str], str]] = {}
    for entry in tree["tree"]:
        if not isinstance(entry, dict):
            continue
        path, blob, size = entry.get("path"), entry.get("sha"), entry.get("size")
        if (
            entry.get("type") == "blob"
            and entry.get("mode") in {"100644", "100755"}
            and isinstance(path, str)
            and len(path) <= 500
            and isinstance(blob, str)
            and re.fullmatch(_SHA, blob)
            and isinstance(size, int)
            and 0 < size <= FILE_BYTES
            and (priority := _path_priority(path, skills, hints)) is not None
        ):
            files[path] = priority, blob
    if len(files) > FILE_LIMIT:
        result.limitations.append("File coverage was limited to the highest-ranked paths.")

    ordered = sorted(files.values())
    metadata = [
        item
        for item in ordered
        if item[0][2].endswith((".yml", ".yaml"))
        or is_manifest_path(item[0][2])
        or is_infrastructure_path(item[0][2])
    ]
    source = [item for item in ordered if item[0][2].endswith(".py")]
    documentation = [item for item in ordered if item[0][2].lower().startswith("readme.")]
    staged = [*metadata[:4], *documentation[:1], *source[:5]]
    chosen = list(dict.fromkeys(staged + ordered))[:FILE_LIMIT]

    seen: set[tuple[tuple[str, ...], Classification, str]] = set()
    for (_, _, path), blob in chosen:
        try:
            signals = await _signals(client, github, project, base, commit, path, blob)
        except RateLimited:
            raise
        except (GitHubError, httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            result.limitations.append("At least one source file could not be verified.")
            continue
        for signal in signals:
            matched = tuple(skill for skill in signal.skills if skill in skills)
            kind = signal.classification
            key = matched, kind, signal.explanation
            if not matched or key in seen:
                continue
            seen.add(key)
            end = max(signal.start, signal.end)
            identity = json.dumps(
                (
                    str(project.id),
                    commit,
                    path,
                    signal.start,
                    end,
                    kind,
                    signal.explanation,
                    EXTRACTOR_VERSION,
                )
            )
            permalink = (
                None
                if project.is_private
                else f"https://github.com/{repository.full_name}/blob/{commit}/{quote(path)}"
                f"#L{signal.start}-L{end}"
            )
            result.findings.append(
                CodeFinding(
                    id=hashlib.sha256(identity.encode()).hexdigest(),
                    skills=matched,
                    project_id=project.id,
                    repository_id=repository.external_id,
                    repository_name=repository.name,
                    full_name=repository.full_name,
                    is_private=repository.is_private,
                    path=path,
                    start_line=signal.start,
                    end_line=end,
                    commit_sha=commit,
                    permalink=permalink,
                    explanation=signal.explanation,
                    classification=kind,
                    can_support_claim=kind is Classification.IMPLEMENTED and bool(signal.wording),
                    wording=signal.wording,
                )
            )


async def search_code(
    projects: Sequence[Project],
    needs: Sequence[SkillMention],
    *,
    github: GitHubClient | None = None,
) -> SearchResult:
    """Read a bounded set of files from relevant imported repositories."""
    github = github or GitHubClient(token=get_settings().github_token, timeout=3.0)
    result = SearchResult()
    candidates = _candidates(projects, needs, allow_private=bool(github.token))
    if not candidates and needs:
        result.limitations.append(
            "No eligible imported GitHub repositories matched the requirements."
        )
    if not github.token and any(p.is_private and eligible(p, allow_private=True) for p in projects):
        result.limitations.append("Private repositories require the configured GitHub token.")
    if len(candidates) > REPO_LIMIT:
        result.limitations.append(
            f"Repository coverage was limited to the top {REPO_LIMIT} matches."
        )
    async with httpx.AsyncClient(
        transport=github._transport,
        timeout=github._timeout,
        headers=github._headers(),
        follow_redirects=False,
    ) as client:
        for project in candidates[:REPO_LIMIT]:
            try:
                async with asyncio.timeout(7):
                    await _scan(client, github, project, needs, result)
            except RateLimited:
                result.limitations.append(
                    "GitHub rate limit reached. Retry after the quota resets."
                )
                break
            except (
                GitHubError,
                httpx.HTTPError,
                TimeoutError,
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
            ):
                result.limitations.append(
                    "At least one GitHub repository could not be fully checked."
                )
    # A checked-in caveat is a reason to ask for clarification, even when an
    # implementation pattern also matched. It cannot authorize a new claim.
    contradicted = {
        (finding.project_id, skill)
        for finding in result.findings
        if finding.classification is Classification.INSUFFICIENT
        for skill in finding.skills
    }
    for finding in result.findings:
        if finding.can_support_claim and any(
            (finding.project_id, skill) in contradicted for skill in finding.skills
        ):
            finding.can_support_claim = False
            finding.wording = ()
            finding.verification_status = "commit verified · conflicting documentation"

    result.limitations = list(dict.fromkeys(result.limitations))
    return result
