"""Résumé upload API, and the résumé reaching the ATS file field."""

from __future__ import annotations

import io
import uuid

import pytest
from httpx import AsyncClient

from packages.core.storage import LocalStorage, set_storage

SAMPLE = b"""Ada Lovelace
ada@example.com | +1 (555) 555-0100

Summary
Backend engineer.

Experience
Staff Engineer, Analytical Engines Ltd

Skills
Python, PostgreSQL
"""


@pytest.fixture(autouse=True)
def _storage(tmp_path):
    set_storage(LocalStorage(tmp_path / "storage"))
    yield
    set_storage(None)


def _upload(name: str = "resume.txt", data: bytes = SAMPLE):
    return {"file": (name, io.BytesIO(data), "text/plain")}


async def test_upload_parses_and_stores(client: AsyncClient, bare_candidate) -> None:
    r = await client.post(
        "/resumes",
        data={"candidate_id": bare_candidate["candidate_id"], "is_default": "true"},
        files=_upload(),
    )

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["is_default"] is True
    assert body["storage_ref"].startswith("resumes/")


async def test_parsed_structure_is_inspectable(client: AsyncClient, complete_candidate) -> None:
    """You can check what the parser saw before trusting an application."""
    created = await client.post(
        "/resumes",
        data={"candidate_id": complete_candidate["candidate_id"]},
        files=_upload(),
    )

    parsed = await client.get(f"/resumes/{created.json()['id']}/parsed")

    assert parsed.status_code == 200
    body = parsed.json()
    assert body["contact"]["email"] == "ada@example.com"
    assert "experience" in body["sections"]
    assert body["line_count"] > 0


async def test_versions_increment(client: AsyncClient, bare_candidate) -> None:
    payload = {"candidate_id": bare_candidate["candidate_id"]}
    first = await client.post("/resumes", data=payload, files=_upload())
    second = await client.post("/resumes", data=payload, files=_upload())

    assert first.json()["version"] == 1
    assert second.json()["version"] == 2


async def test_only_one_default(client: AsyncClient, complete_candidate) -> None:
    payload = {"candidate_id": complete_candidate["candidate_id"], "is_default": "true"}
    await client.post("/resumes", data=payload, files=_upload())
    await client.post("/resumes", data=payload, files=_upload())

    listing = await client.get(
        "/resumes", params={"candidate_id": complete_candidate["candidate_id"]}
    )
    defaults = [r for r in listing.json() if r["is_default"]]
    assert len(defaults) == 1


async def test_unreadable_file_is_rejected_at_upload(
    client: AsyncClient, complete_candidate
) -> None:
    """Fail while the owner is watching, not mid-application later."""
    r = await client.post(
        "/resumes",
        data={"candidate_id": complete_candidate["candidate_id"]},
        files=_upload("resume.pdf", b"not actually a pdf"),
    )

    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


async def test_unsupported_format_is_rejected(client: AsyncClient, complete_candidate) -> None:
    r = await client.post(
        "/resumes",
        data={"candidate_id": complete_candidate["candidate_id"]},
        files=_upload("resume.pages", b"x"),
    )
    assert r.status_code == 400
    assert "unsupported" in r.json()["error"]["message"]


async def test_empty_file_is_rejected(client: AsyncClient, complete_candidate) -> None:
    r = await client.post(
        "/resumes",
        data={"candidate_id": complete_candidate["candidate_id"]},
        files=_upload("resume.txt", b""),
    )
    assert r.status_code == 400


async def test_unknown_candidate_is_rejected(client: AsyncClient) -> None:
    r = await client.post("/resumes", data={"candidate_id": str(uuid.uuid4())}, files=_upload())
    assert r.status_code == 400


async def test_set_as_profile_base(client: AsyncClient, complete_candidate) -> None:
    created = await client.post(
        "/resumes",
        data={"candidate_id": complete_candidate["candidate_id"]},
        files=_upload(),
    )

    r = await client.post(
        f"/resumes/{created.json()['id']}/set-base",
        params={"profile_id": complete_candidate["profile_id"]},
    )

    assert r.status_code == 200


async def test_set_base_rejects_a_foreign_resume(client: AsyncClient, complete_candidate) -> None:
    other = await client.post("/candidates", json={"name": "Other", "email": "other-r@example.com"})
    created = await client.post(
        "/resumes", data={"candidate_id": other.json()["id"]}, files=_upload()
    )

    r = await client.post(
        f"/resumes/{created.json()['id']}/set-base",
        params={"profile_id": complete_candidate["profile_id"]},
    )

    assert r.status_code == 400
    assert "different candidate" in r.json()["error"]["message"]


async def test_file_can_be_downloaded(client: AsyncClient, complete_candidate) -> None:
    created = await client.post(
        "/resumes",
        data={"candidate_id": complete_candidate["candidate_id"]},
        files=_upload(),
    )

    r = await client.get(f"/resumes/{created.json()['id']}/file")

    assert r.status_code == 200
    assert b"Ada Lovelace" in r.content


# --------------------------------------------------------------------------
# The completeness gate now requires a résumé
# --------------------------------------------------------------------------


async def test_application_without_a_resume_is_rejected(
    client: AsyncClient, bare_candidate
) -> None:
    """Every ATS form has a required résumé field, so this cannot succeed."""
    r = await client.post(
        "/applications",
        json={**bare_candidate, "url": "https://boards.greenhouse.io/acme/jobs/1"},
    )

    assert r.status_code == 400
    assert "profile.base_resume_id" in r.json()["error"]["message"]


async def test_application_succeeds_once_a_resume_is_attached(
    client: AsyncClient, complete_candidate
) -> None:
    created = await client.post(
        "/resumes",
        data={"candidate_id": complete_candidate["candidate_id"]},
        files=_upload(),
    )
    await client.post(
        f"/resumes/{created.json()['id']}/set-base",
        params={"profile_id": complete_candidate["profile_id"]},
    )

    r = await client.post(
        "/applications",
        json={**complete_candidate, "url": "https://boards.greenhouse.io/acme/jobs/2"},
    )

    assert r.status_code == 201
