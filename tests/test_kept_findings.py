"""Studio's implementation findings outlive the preview that made them.

A Resume Studio save re-checks a repository's code before it keeps an
approved claim. The public, commit-verified findings of that check are kept on
the saved draft (`kept_findings.kept`) so a skill-gap answer can cite the file
that implements a skill (`kept_findings.implemented`). What must hold: only a
public finding that can support a claim and is pinned to its own commit is
kept; no wording or source text is kept; a malformed entry is never trusted;
the same finding saved twice is cited once.
"""

from __future__ import annotations

import uuid

from packages.core.models import Resume
from packages.github.code_search import CodeFinding
from packages.github.code_signals import Classification
from packages.tailor.kept_findings import (
    CITATIONS_PER_SKILL,
    FINDINGS_KEY,
    STUDIO_STAMP,
    implemented,
    kept,
)

SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "f" * 40
PROJECT = uuid.uuid4()


def finding(n: int = 1, **changes: object) -> CodeFinding:
    path = changes.pop("path", f"app/routes_{n}.py")
    values: dict[str, object] = dict(
        id=f"{n:064x}",
        skills=("fastapi", "rest"),
        project_id=PROJECT,
        repository_id="17",
        repository_name="api",
        full_name="example/api",
        is_private=False,
        path=path,
        start_line=10,
        end_line=24,
        commit_sha=SHA,
        permalink=f"https://github.com/example/api/blob/{SHA}/{path}#L10-L24",
        explanation="An HTTP route is bound to a FastAPI app or router and has a handler body.",
        classification=Classification.IMPLEMENTED,
        can_support_claim=True,
        wording=("Implemented HTTP API routes with FastAPI.",),
    )
    return CodeFinding(**(values | changes))


def draft(*entries: dict, version: int = 1, stamp: object = None) -> Resume:
    parsed: dict[str, object] = {"raw_lines": ["Built services"]}
    parsed[STUDIO_STAMP] = stamp if stamp is not None else {FINDINGS_KEY: list(entries)}
    return Resume(
        id=uuid.uuid4(),
        candidate_id=uuid.uuid4(),
        storage_ref="studio.pdf",
        version=version,
        parsed_json=parsed,
    )


# --- what a save keeps ----------------------------------------------------------------


def test_only_public_claim_supporting_findings_pinned_to_their_commit_are_kept():
    good = finding(1)
    private = finding(2, is_private=True, permalink=None)
    declared = finding(3, classification=Classification.DECLARED, can_support_claim=False)
    contradicted = finding(4, can_support_claim=False, wording=())
    off_commit = finding(
        5, permalink=f"https://github.com/example/api/blob/{OTHER_SHA}/app/routes_5.py#L10-L24"
    )

    saved = kept([good, private, declared, contradicted, off_commit])

    assert [entry["id"] for entry in saved] == [good.id]


def test_a_kept_finding_carries_its_citation_and_no_wording_or_source_text():
    (entry,) = kept([finding(1)])

    assert set(entry) == {
        "id",
        "project_id",
        "skills",
        "path",
        "start_line",
        "end_line",
        "commit_sha",
        "url",
        "explanation",
    }
    assert entry["project_id"] == str(PROJECT)
    assert entry["url"].startswith(f"https://github.com/example/api/blob/{SHA}/")


# --- what an answer reads back ---------------------------------------------------------


def test_every_saved_draft_is_read_and_a_finding_saved_twice_is_cited_once():
    first, second = kept([finding(1)]), kept([finding(1), finding(2)])

    found = implemented([draft(*first), draft(*second, version=2)])

    assert set(found) == {(PROJECT, "fastapi"), (PROJECT, "rest")}
    assert [c.path for c in found[(PROJECT, "fastapi")]] == ["app/routes_1.py", "app/routes_2.py"]


def test_a_resume_without_kept_findings_contributes_nothing():
    plain = Resume(
        id=uuid.uuid4(),
        candidate_id=uuid.uuid4(),
        storage_ref="a.pdf",
        version=1,
        parsed_json={"raw_lines": ["x"]},
    )
    empty = Resume(
        id=uuid.uuid4(), candidate_id=uuid.uuid4(), storage_ref="b.pdf", version=2, parsed_json=None
    )

    assert implemented([plain, empty, draft(stamp="not a mapping"), draft()]) == {}


def test_a_malformed_kept_finding_is_skipped_and_never_trusted():
    (good,) = kept([finding(1)])
    unpinned = good | {"id": "2" * 64, "url": "https://github.com/example/api/blob/main/x.py"}
    short_sha = good | {"id": "3" * 64, "commit_sha": "abc"}
    elsewhere = good | {"id": "4" * 64, "url": f"https://evil.example/{SHA}/x.py"}
    backwards = good | {"id": "5" * 64, "start_line": 30, "end_line": 2}

    found = implemented([draft(unpinned, short_sha, elsewhere, backwards, "text", good)])

    assert [c.path for c in found[(PROJECT, "fastapi")]] == ["app/routes_1.py"]


def test_citations_per_skill_are_ordered_and_capped():
    many = kept([finding(n, path=f"app/{chr(ord('z') - n)}.py") for n in range(1, 7)])

    cited = implemented([draft(*many)])[(PROJECT, "fastapi")]

    assert len(cited) == CITATIONS_PER_SKILL
    assert [c.path for c in cited] == sorted(c.path for c in cited)
