"""Studio routes: evidence in tables, a preview that writes nothing, a save that judges every line.

Against the test database through the real app. No route here calls a model:
`use_model` is left off, and the model path is covered offline in
`test_resume_studio.py`.
"""

from __future__ import annotations

import io
import uuid

from docx import Document
from pypdf import PdfReader
from sqlalchemy import func, select

from packages.core.models import Project

INTERNSHIP = {
    "company": "Acme Robotics",
    "role": "Software Engineering Intern",
    "started": "Jun 2025",
    "ended": "Aug 2025",
    "stack": ["Python", "FastAPI", "Docker"],
    "bullets": ["Built a telemetry pipeline that cut report time by 40%."],
    "verified": True,
}
DRAFT_INTERNSHIP = {**INTERNSHIP, "company": "Unconfirmed Labs", "verified": False}
CERTIFICATION = {
    "name": "Certified Kubernetes Administrator",
    "issuer": "The Linux Foundation",
    "issued": "Mar 2025",
    "skills": ["Kubernetes", "Helm"],
    "verified": True,
}
POSTING = "Requirements:\nPython, FastAPI, Docker, Kubernetes and Helm for platform services.\n"


async def _save_evidence(client, profile_id: str) -> dict:  # noqa: ANN001
    response = await client.put(
        f"/profiles/{profile_id}/evidence",
        json={"internships": [INTERNSHIP, DRAFT_INTERNSHIP], "certifications": [CERTIFICATION]},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _resumes(client, candidate_id: str) -> list[dict]:  # noqa: ANN001
    return (await client.get("/resumes", params={"candidate_id": candidate_id})).json()


async def test_evidence_is_kept_in_order_and_replaced_whole(client, complete_candidate) -> None:
    profile_id = complete_candidate["profile_id"]
    saved = await _save_evidence(client, profile_id)
    assert [i["company"] for i in saved["internships"]] == ["Acme Robotics", "Unconfirmed Labs"]

    loaded = (await client.get(f"/profiles/{profile_id}/evidence")).json()
    assert loaded == saved

    cleared = await client.put(
        f"/profiles/{profile_id}/evidence", json={"internships": [], "certifications": []}
    )
    assert cleared.json()["internships"] == [] and cleared.json()["certifications"] == []


async def test_evidence_is_validated(client, complete_candidate) -> None:
    profile_id = complete_candidate["profile_id"]
    response = await client.put(
        f"/profiles/{profile_id}/evidence",
        json={"internships": [{**INTERNSHIP, "company": ""}], "certifications": []},
    )
    assert response.status_code == 422 or response.json()["error"]["code"] == "invalid_request"


async def test_a_preview_writes_nothing_and_adds_only_confirmed_records(
    client, complete_candidate
) -> None:
    candidate_id, profile_id = complete_candidate["candidate_id"], complete_candidate["profile_id"]
    await _save_evidence(client, profile_id)
    before = (await client.get(f"/profiles/{profile_id}")).json()
    resumes_before = await _resumes(client, candidate_id)

    response = await client.post(
        "/resumes/synthesize",
        json={
            "candidate_id": candidate_id,
            "profile_id": profile_id,
            "job_description": POSTING,
            "role_track": "cloud_devops",
        },
    )
    assert response.status_code == 200, response.text
    preview = response.json()

    experience = "\n".join(preview["sections"]["experience"])
    assert "Acme Robotics" in experience and "Unconfirmed Labs" not in experience
    assert preview["sections"]["certifications"][0].startswith("Certified Kubernetes Administrator")
    origins = {line["origin"] for line in preview["lines"]["experience"]}
    assert {"resume", "internship"} <= origins
    assert preview["ats_after"]["keywords"] > preview["ats_before"]["keywords"]
    assert preview["calls"] == 0 and preview["tailored_by"] is None
    assert "Cloud / DevOps" not in preview["html"]

    assert (await client.get(f"/profiles/{profile_id}")).json() == before
    assert await _resumes(client, candidate_id) == resumes_before


async def test_a_saved_draft_is_a_new_rendered_version_and_can_be_adopted(
    client, complete_candidate, committing_sessionmaker
) -> None:
    candidate_id, profile_id = complete_candidate["candidate_id"], complete_candidate["profile_id"]
    await _save_evidence(client, profile_id)
    async with committing_sessionmaker() as session:
        repo = Project(
            candidate_id=uuid.UUID(candidate_id),
            external_id="1",
            name="platform-kit",
            url="https://github.com/octocat/platform-kit",
            description="Helm charts for small services",
            language="Python",
            topics_json=["kubernetes"],
        )
        session.add(repo)
        await session.commit()
        repo_id = str(repo.id)

    preview = (
        await client.post(
            "/resumes/synthesize",
            json={
                "candidate_id": candidate_id,
                "profile_id": profile_id,
                "job_description": POSTING,
                "selected_project_ids": [repo_id],
            },
        )
    ).json()
    assert [p["name"] for p in preview["projects"] if p["chosen"]] == ["platform-kit"]

    edited = {**preview["sections"]}
    edited["experience"] = [*edited["experience"], "Mentored two new hires."]
    saved = await client.post(
        "/resumes/synthesize/save",
        json={
            "candidate_id": candidate_id,
            "profile_id": profile_id,
            "base_resume_id": preview["base_resume_id"],
            "selected_project_ids": [repo_id],
            "contact": preview["contact"],
            "sections": edited,
            "owner_edits": ["Mentored two new hires."],
            "adopt": True,
        },
    )
    assert saved.status_code == 201, saved.text
    resume = saved.json()["resume"]
    assert saved.json()["adopted"] is True
    assert (await client.get(f"/profiles/{profile_id}")).json()["base_resume_id"] == resume["id"]

    pdf = await client.get(f"/resumes/{resume['id']}/file")
    text = " ".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf.content)).pages)
    assert "Acme Robotics" in text and "platform-kit" in text and "Mentored two new hires" in text

    exported = await client.get(f"/resumes/{resume['id']}/docx")
    document = Document(io.BytesIO(exported.content))
    assert not document.tables
    words = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "platform-kit — Helm charts for small services" in words

    parsed = (await client.get(f"/resumes/{resume['id']}/parsed")).json()["parsed"]
    assert "Mentored two new hires." in parsed["raw_lines"]
    assert parsed["resume_studio"]["project_ids"] == [repo_id]


async def test_a_line_nothing_backs_is_refused_and_nothing_is_saved(
    client, complete_candidate, committing_sessionmaker
) -> None:
    candidate_id, profile_id = complete_candidate["candidate_id"], complete_candidate["profile_id"]
    preview = (
        await client.post(
            "/resumes/synthesize",
            json={"candidate_id": candidate_id, "job_description": POSTING},
        )
    ).json()
    count = len(await _resumes(client, candidate_id))

    sections = {**preview["sections"]}
    sections["experience"] = [*sections["experience"], "Led a Kubernetes migration for 40 teams."]
    refused = await client.post(
        "/resumes/synthesize/save",
        json={
            "candidate_id": candidate_id,
            "profile_id": profile_id,
            "base_resume_id": preview["base_resume_id"],
            "contact": preview["contact"],
            "sections": sections,
            "rewrites": [
                {
                    "original": "Staff Engineer, Analytical Engines Ltd",
                    "tailored": "Led a Kubernetes migration for 40 teams.",
                }
            ],
            "adopt": True,
        },
    )
    assert refused.status_code == 400
    assert "Led a Kubernetes migration" in refused.json()["error"]["message"]
    assert len(await _resumes(client, candidate_id)) == count


async def test_an_evidence_only_private_repository_cannot_be_printed(
    client, complete_candidate, committing_sessionmaker
) -> None:
    candidate_id = complete_candidate["candidate_id"]
    async with committing_sessionmaker() as session:
        hidden = Project(
            candidate_id=uuid.UUID(candidate_id),
            external_id="2",
            name="secret-harness",
            url="https://github.com/octocat/secret-harness",
            description="Private evaluation harness",
            is_private=True,
        )
        session.add(hidden)
        await session.commit()
        hidden_id = str(hidden.id)
        before = await session.scalar(select(func.count()).select_from(Project))

    preview = (
        await client.post(
            "/resumes/synthesize",
            json={
                "candidate_id": candidate_id,
                "job_description": POSTING,
                "selected_project_ids": [hidden_id],
            },
        )
    ).json()
    pick = next(p for p in preview["projects"] if p["id"] == hidden_id)
    assert not pick["chosen"] and pick["held_back"] == "evidence only"
    assert "secret-harness" not in preview["html"]

    refused = await client.post(
        "/resumes/synthesize/save",
        json={
            "candidate_id": candidate_id,
            "base_resume_id": preview["base_resume_id"],
            "selected_project_ids": [hidden_id],
            "contact": preview["contact"],
            "sections": preview["sections"],
        },
    )
    assert refused.status_code == 400 and "evidence only" in refused.json()["error"]["message"]
    async with committing_sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(Project)) == before


async def test_a_studio_request_for_another_candidates_resume_is_refused(
    client, complete_candidate
) -> None:
    response = await client.post(
        "/resumes/synthesize",
        json={
            "candidate_id": complete_candidate["candidate_id"],
            "base_resume_id": str(uuid.uuid4()),
            "job_description": POSTING,
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
