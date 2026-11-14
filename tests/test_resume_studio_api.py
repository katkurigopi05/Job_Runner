"""Studio HTTP contracts: evidence preservation, real exports, and adoption."""

from __future__ import annotations

import io
import uuid

from docx import Document
from docx.oxml.ns import qn
from pypdf import PdfReader

from packages.core.models import Profile


async def test_evidence_roundtrip_preserves_profile_answers(
    client, complete_candidate, committing_sessionmaker
):
    profile_id = complete_candidate["profile_id"]
    await client.patch(f"/profiles/{profile_id}", json={"answers": {"availability": "Two weeks"}})
    evidence = {
        "profile_id": profile_id,
        "internships": [{"company": "Example Labs", "role": "Engineering Intern"}],
        "certifications": [{"name": "Example Cloud Certification"}],
    }
    saved = await client.put("/resumes/studio-evidence", json=evidence)
    assert saved.status_code == 200, saved.text
    loaded = await client.get("/resumes/studio-evidence", params={"profile_id": profile_id})
    assert loaded.status_code == 200 and loaded.json() == saved.json()
    async with committing_sessionmaker() as session:
        profile = await session.get(Profile, uuid.UUID(profile_id))
        assert profile.answers_kv_json["availability"] == "Two weeks"
        assert (
            profile.answers_kv_json["resume_studio"]["internships"][0]["company"] == "Example Labs"
        )


async def test_synthesis_adopts_one_default_and_exports_pdf_docx(client, complete_candidate):
    candidate_id = complete_candidate["candidate_id"]
    initial = (await client.get("/resumes", params={"candidate_id": candidate_id})).json()[0]
    # Set a prior default to reproduce the old duplicate-default integration bug.
    uploaded = await client.post(
        "/resumes",
        data={"candidate_id": candidate_id, "is_default": "true"},
        files={
            "file": ("resume.txt", b"Alex Rivera\nalex@example.com\nSkills\nPython", "text/plain")
        },
    )
    assert uploaded.status_code == 201, uploaded.text
    response = await client.post(
        "/resumes/synthesize",
        json={
            **complete_candidate,
            "base_resume_id": initial["id"],
            "job_description": "Python engineer",
            "save_version": True,
            "adopt": True,
        },
    )
    assert response.status_code == 200, response.text
    saved = response.json()["saved_resume"]
    assert saved["version"] == uploaded.json()["version"] + 1
    profile = (await client.get(f"/profiles/{complete_candidate['profile_id']}")).json()
    assert profile["base_resume_id"] == saved["id"]
    listing = (await client.get("/resumes", params={"candidate_id": candidate_id})).json()
    assert [row["id"] for row in listing if row["is_default"]] == [saved["id"]]
    pdf = await client.get(f"/resumes/{saved['id']}/file")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert len(PdfReader(io.BytesIO(pdf.content)).pages) >= 1
    exported = await client.get(f"/resumes/{saved['id']}/docx")
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    document = Document(io.BytesIO(exported.content))
    assert not document.tables
    for section in document.sections:
        columns = section._sectPr.find(qn("w:cols"))
        assert columns is None or columns.get(qn("w:num"), "1") == "1"


async def test_preview_creates_no_version_or_profile_change(client, complete_candidate):
    profile_id = complete_candidate["profile_id"]
    before = (await client.get(f"/profiles/{profile_id}")).json()
    response = await client.post(
        "/resumes/synthesize",
        json={
            **complete_candidate,
            "job_description": "Python engineer",
            "save_version": False,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["saved_resume"] is None
    after = (await client.get(f"/profiles/{profile_id}")).json()
    assert before == after
    listing = await client.get(
        "/resumes", params={"candidate_id": complete_candidate["candidate_id"]}
    )
    assert len(listing.json()) == 1
