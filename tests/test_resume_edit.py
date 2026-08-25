"""Editing a parsed résumé, and the three ways that goes silently wrong.

The parsed form was read-only: fixing a typo meant editing the source document
elsewhere and re-uploading. This is the smaller path. What makes it more than a
form save is that a résumé is referenced from three directions at once — the
stored file an application uploads, the `parsed_json` tailoring renders from,
and the `raw_lines` the fabrication guard checks against — and an edit that
updates one of them is worse than no edit at all.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from packages.tailor.edit import apply_edit, rebuild_raw_lines
from packages.tailor.parse import Contact, ParsedResume

RESUME = """Jane Doe
jane@example.com

Experience

Built the billing service in Python.

Skills

Python, PostgreSQL
"""


def _parsed() -> ParsedResume:
    return ParsedResume(
        contact=Contact(name="Jane Doe", email="jane@example.com"),
        sections={"experience": ["Built the billing service in Python."], "skills": ["Python"]},
        raw_lines=["Jane Doe", "jane@example.com", "Built the billing service in Python."],
    )


def test_the_guard_corpus_follows_the_edit() -> None:
    """The subtle one, and the reason this is not a plain form save.

    `raw_lines` is what the fabrication guard treats as "was this in the
    source". An edit that adds a real employer while leaving it stale would
    have the guard refuse the owner's own fact during tailoring, and the
    rewriter would look broken while behaving exactly as designed.
    """
    edited = apply_edit(
        _parsed(),
        contact=Contact(name="Jane Doe", email="jane@example.com"),
        sections={
            "experience": ["Built the billing service in Python.", "Led the migration at Acme."],
            "skills": ["Python"],
        },
    )

    assert "Led the migration at Acme." in edited.raw_lines
    assert "Led the migration at Acme." in edited.text


def test_a_cleared_section_is_removed_not_left_empty() -> None:
    """An empty key would print a bare heading onto the PDF."""
    edited = apply_edit(
        _parsed(),
        contact=Contact(name="Jane Doe"),
        sections={"experience": ["Built the billing service in Python."], "skills": ["  ", ""]},
    )

    assert "skills" not in edited.sections


def test_a_section_the_editor_never_showed_survives() -> None:
    """Losing the owner's text because a form did not render it is deletion."""
    source = _parsed()
    source.preamble = ["Available from June."]

    edited = apply_edit(
        source,
        contact=Contact(name="Jane Doe"),
        sections={"experience": ["Built the billing service in Python."]},
    )

    assert edited.preamble == ["Available from June."]
    assert "Available from June." in edited.raw_lines


def test_rebuilt_lines_keep_section_headings() -> None:
    """A corpus without them stops recognising "Experience" as source text."""
    rebuilt = rebuild_raw_lines(_parsed())

    assert "experience" in rebuilt.raw_lines


# --------------------------------------------------------------------------
# The route
# --------------------------------------------------------------------------


async def _uploaded(client: AsyncClient) -> tuple[str, str]:
    """A candidate with one uploaded résumé. Returns (candidate_id, resume_id)."""
    suffix = uuid.uuid4().hex[:8]
    candidate = await client.post(
        "/candidates", json={"name": "Jane Doe", "email": f"j-{suffix}@example.com"}
    )
    candidate_id = candidate.json()["id"]

    uploaded = await client.post(
        "/resumes",
        data={"candidate_id": candidate_id, "is_default": "true"},
        files={"file": ("resume.txt", RESUME.encode(), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    return candidate_id, uploaded.json()["id"]


@pytest.mark.asyncio
async def test_an_edit_creates_a_new_version_rather_than_rewriting_history(
    client: AsyncClient,
) -> None:
    """An application may already have sent the source résumé.

    Mutating the row would leave a receipt describing a document that no longer
    exists, which is the one thing an audit trail must not do.
    """
    candidate_id, resume_id = await _uploaded(client)

    edited = await client.post(
        f"/resumes/{resume_id}/edit",
        json={
            "contact": {"name": "Jane Doe", "email": "jane@example.com"},
            "sections": {"experience": ["Built the billing service in Python and Go."]},
        },
    )

    assert edited.status_code == 201, edited.text
    body = edited.json()
    assert body["id"] != resume_id
    assert body["version"] > 1

    # The source is untouched and still readable.
    original = await client.get(f"/resumes/{resume_id}/parsed")
    assert original.status_code == 200
    assert "Go" not in str(original.json()["parsed"])


@pytest.mark.asyncio
async def test_the_edit_is_rendered_to_a_file_of_its_own(client: AsyncClient) -> None:
    """`_resume_path` uploads the *file*, so an unrendered edit is invisible.

    Storing only `parsed_json` would make the change show up on tailored
    applications and not on untailored ones — the same divergence that let the
    base résumé go out while the review screen showed a tailored diff.
    """
    _, resume_id = await _uploaded(client)

    edited = await client.post(
        f"/resumes/{resume_id}/edit",
        json={
            "contact": {"name": "Jane Doe"},
            "sections": {"experience": ["Built the billing service in Python and Go."]},
        },
    )
    new_id = edited.json()["id"]

    downloaded = await client.get(f"/resumes/{new_id}/file")

    assert downloaded.status_code == 200
    assert downloaded.content[:4] == b"%PDF"


@pytest.mark.asyncio
async def test_adopting_moves_the_profiles_that_used_the_source(client: AsyncClient) -> None:
    """Otherwise the edit changes nothing and the screen cannot say so.

    Profiles keep their old `base_resume_id`, applications keep sending the old
    file, and a silent no-op is the failure this project keeps having.
    """
    candidate_id, resume_id = await _uploaded(client)
    profile = await client.post(
        "/profiles", json={"candidate_id": candidate_id, "label": "default"}
    )
    assert profile.status_code == 201, profile.text
    profile_id = profile.json()["id"]

    # `ProfileCreate` has no base_resume_id — linking is what set-base is for.
    linked = await client.post(f"/resumes/{resume_id}/set-base?profile_id={profile_id}")
    assert linked.status_code == 200, linked.text

    edited = await client.post(
        f"/resumes/{resume_id}/edit",
        json={
            "contact": {"name": "Jane Doe"},
            "sections": {"experience": ["Built the billing service in Python and Go."]},
            "adopt": True,
        },
    )
    new_id = edited.json()["id"]

    after = await client.get(f"/profiles/{profile_id}")
    assert after.json()["base_resume_id"] == new_id


@pytest.mark.asyncio
async def test_an_empty_edit_is_refused(client: AsyncClient) -> None:
    """A résumé with nothing in it renders a blank PDF an employer receives."""
    _, resume_id = await _uploaded(client)

    edited = await client.post(
        f"/resumes/{resume_id}/edit",
        json={"contact": {}, "sections": {}},
    )

    assert edited.status_code == 400
    assert edited.json()["error"]["code"] == "invalid_request"
