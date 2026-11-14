"""Resume Studio — synthesize role-targeted résumés from grounded candidate evidence.

Combines:
- The candidate's parsed base résumé (education, prior experience, contact details)
- Curated GitHub repositories (both private and public synced via `GITHUB_TOKEN`)
- Verified internships and certifications stored in the owner's studio profile
- A pasted target job description and role track (`swe`, `fullstack`, `data_ml`,
  `ai_agentic`, `cloud_devops`, `fde_harness`)

Every technology, metric, internship, and certification stays traceable to the
candidate's actual records. Optional LLM bullet rewriting still flows through
`rewrite.tailor_bullets` and `guard.check`, falling back to originals if any
claim is unsupported.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, Field

from packages.core.models import Candidate, Profile, Project
from packages.github.select import select_projects
from packages.llm.provider import LLMProvider
from packages.tailor import ats, edit
from packages.tailor.assemble import AssemblyOptions, assemble_html
from packages.tailor.bullets import tailorable_bullets
from packages.tailor.guard import SourceCorpus, normalize
from packages.tailor.keywords import analyze
from packages.tailor.parse import Contact, ParsedResume
from packages.tailor.publish import _with_ordered_skills, apply_rewrites
from packages.tailor.rewrite import tailor_bullets


class RoleTrack(StrEnum):
    """Target engineering role presets supported by Resume Studio."""

    SWE = "swe"
    FULLSTACK = "fullstack"
    DATA_ML = "data_ml"
    AI_AGENTIC = "ai_agentic"
    CLOUD_DEVOPS = "cloud_devops"
    FDE_HARNESS = "fde_harness"


ROLE_TRACK_META: dict[RoleTrack, dict[str, str]] = {
    RoleTrack.SWE: {
        "label": "Software Engineer",
        "headline": "Software Engineer focusing on backend systems, testing, and production APIs",
    },
    RoleTrack.FULLSTACK: {
        "label": "Full-Stack Engineer",
        "headline": (
            "Full-Stack Engineer building end-to-end web interfaces, APIs, and data layers"
        ),
    },
    RoleTrack.DATA_ML: {
        "label": "Data / ML Engineer",
        "headline": (
            "Data & ML Engineer building retrieval pipelines, feature workflows, "
            "and model evaluation"
        ),
    },
    RoleTrack.AI_AGENTIC: {
        "label": "AI / Agentic AI Engineer",
        "headline": (
            "AI & Agentic Systems Engineer building LLM workflows, tool use, and evaluation guards"
        ),
    },
    RoleTrack.CLOUD_DEVOPS: {
        "label": "Cloud / DevOps Engineer",
        "headline": (
            "Cloud & DevOps Engineer focused on containers, CI/CD automation, "
            "and infrastructure reliability"
        ),
    },
    RoleTrack.FDE_HARNESS: {
        "label": "FDE / Harness Engineer",
        "headline": (
            "Forward Deployed & Agent Harness Engineer integrating customer systems, "
            "evals, and guardrails"
        ),
    },
}


class InternshipRecord(BaseModel):
    """Verified internship or co-op experience provided by the candidate."""

    company: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=200)
    dates: str = Field(default="", max_length=100)
    location: str = Field(default="", max_length=120)
    stack: list[str] = Field(default_factory=list)
    bullets: list[str] = Field(default_factory=list)


class CertificationRecord(BaseModel):
    """Verified technical certification earned by the candidate."""

    name: str = Field(min_length=1, max_length=240)
    issuer: str = Field(default="", max_length=160)
    date: str = Field(default="", max_length=80)
    credential_id: str = Field(default="", max_length=120)
    skills: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class StudioSynthesis:
    """Output of synthesizing a role-targeted résumé against a JD."""

    draft: ParsedResume
    role_track: RoleTrack
    matched_terms: list[str] = field(default_factory=list)
    missing_terms: list[str] = field(default_factory=list)
    ats_parse_score: float = 0.0
    ats_keyword_score: float = 0.0
    ats_findings: list[str] = field(default_factory=list)
    selected_projects: list[str] = field(default_factory=list)
    html_preview: str = ""
    bullets_rewritten: int = 0
    bullets_refused: int = 0


def _merge_contact(
    base: ParsedResume | None,
    candidate: Candidate | None,
    profile: Profile | None,
) -> Contact:
    base_contact = base.contact if base else Contact()
    links: list[str] = []
    seen_links: set[str] = set()

    profile_links = list((profile.links_json or {}).values()) if profile else []
    for raw in list(base_contact.links) + profile_links:
        if not isinstance(raw, str):
            continue
        cleaned = raw.strip()
        if cleaned and cleaned.lower() not in seen_links:
            seen_links.add(cleaned.lower())
            links.append(cleaned)

    return Contact(
        name=base_contact.name or (candidate.name if candidate else None),
        email=base_contact.email or (candidate.email if candidate else None),
        phone=base_contact.phone or (profile.phone if profile else None),
        links=links,
    )


def _format_internships(internships: Sequence[InternshipRecord]) -> list[str]:
    lines: list[str] = []
    for item in internships:
        header = f"{item.role}, {item.company}"
        if item.location.strip():
            header = f"{header}, {item.location.strip()}"
        if item.dates.strip():
            header = f"{header} — {item.dates.strip()}"
        lines.append(header)
        stack = [s.strip() for s in item.stack if s.strip()]
        if stack:
            lines.append(", ".join(stack))
        for bullet in item.bullets:
            text = bullet.strip()
            if text:
                lines.append(text)
    return lines


def _project_lines(project: Project, job_description: str) -> tuple[list[str], list[str]]:
    techs: list[str] = []
    seen: set[str] = set()
    for raw in [
        project.language or "",
        *(project.topics_json or []),
        *(project.dependency_technologies or []),
    ]:
        cleaned = raw.strip()
        key = normalize(cleaned)
        if cleaned and key and key not in seen:
            seen.add(key)
            techs.append(cleaned)

    title = project.name if project.is_private else f"{project.name}   [GitHub]"
    lines = [title]
    if techs:
        lines.append(", ".join(techs[:10]))

    desc = (project.description or "").strip()
    if desc:
        if not desc.endswith((".", "!", "?")):
            desc = f"{desc}."
        lines.append(desc)
    elif techs:
        lines.append(f"Built {project.name} using {', '.join(techs[:5])}.")

    return lines, techs


def _format_certifications(
    certifications: Sequence[CertificationRecord],
) -> tuple[list[str], list[str]]:
    lines: list[str] = []
    skills: list[str] = []
    for cert in certifications:
        title = cert.name.strip()
        if cert.issuer.strip():
            title = f"{title} — {cert.issuer.strip()}"
        if cert.date.strip():
            title = f"{title} ({cert.date.strip()})"
        lines.append(title)
        detail_bits: list[str] = []
        if cert.credential_id.strip():
            detail_bits.append(f"Credential ID: {cert.credential_id.strip()}")
        cert_skills = [s.strip() for s in cert.skills if s.strip()]
        if cert_skills:
            detail_bits.append(f"Skills: {', '.join(cert_skills)}")
            skills.extend(cert_skills)
        if detail_bits:
            lines.append(" · ".join(detail_bits))
    return lines, skills


def _merge_skills(
    base_skills: Sequence[str],
    extra_skills: Sequence[str],
) -> list[str]:
    existing_tokens: set[str] = set()
    flat_existing: list[str] = [line.strip() for line in base_skills if line.strip()]
    for line in flat_existing:
        for item in line.replace(":", ",").split(","):
            tok = normalize(item)
            if tok:
                existing_tokens.add(tok)

    additions: list[str] = []
    for raw in extra_skills:
        cleaned = raw.strip()
        tok = normalize(cleaned)
        if cleaned and tok and tok not in existing_tokens:
            existing_tokens.add(tok)
            additions.append(cleaned)

    if not additions:
        return flat_existing
    if not flat_existing:
        return [", ".join(additions)]
    return [*flat_existing, f"Verified Stack: {', '.join(additions)}"]


async def synthesize_resume(
    *,
    job_description: str,
    role_track: RoleTrack = RoleTrack.SWE,
    candidate: Candidate | None = None,
    profile: Profile | None = None,
    base_resume: ParsedResume | None = None,
    projects: Sequence[Project] = (),
    internships: Sequence[InternshipRecord] = (),
    certifications: Sequence[CertificationRecord] = (),
    linked_evidence: Mapping[str, str] | None = None,
    provider: LLMProvider | None = None,
    use_llm: bool = False,
    project_limit: int = 4,
) -> StudioSynthesis:
    """Synthesize a role-targeted, ATS-checked résumé draft from verified evidence."""
    contact = _merge_contact(base_resume, candidate, profile)
    sections: dict[str, list[str]] = {}
    extra_skills: list[str] = []

    base_summary = [
        line.strip()
        for line in (base_resume.section("summary") if base_resume else [])
        if line.strip()
    ]
    role_headline = ROLE_TRACK_META[role_track]["headline"]
    sections["summary"] = base_summary or [f"{role_headline}."]

    # Experience: verified studio internships + existing base experience (deduplicated by line)
    exp_lines: list[str] = _format_internships(internships)
    for item in internships:
        extra_skills.extend(item.stack)
    seen_exp = {line.casefold() for line in exp_lines}
    if base_resume:
        for line in base_resume.section("experience"):
            cleaned = line.strip()
            if cleaned and cleaned.casefold() not in seen_exp:
                seen_exp.add(cleaned.casefold())
                exp_lines.append(cleaned)
    if exp_lines:
        sections["experience"] = exp_lines

    # Projects: selected or ranked private & public GitHub repositories + existing base projects
    chosen_projects = select_projects(list(projects), job_description, limit=project_limit)
    proj_lines: list[str] = []
    selected_project_names: list[str] = []
    seen_proj_titles: set[str] = set()
    for project in chosen_projects:
        lines, techs = _project_lines(project, job_description)
        proj_lines.extend(lines)
        selected_project_names.append(project.name)
        seen_proj_titles.add(project.name.casefold())
        extra_skills.extend(techs)

    if base_resume:
        for line in base_resume.section("projects"):
            cleaned = line.strip()
            if cleaned and cleaned.casefold() not in seen_proj_titles:
                proj_lines.append(cleaned)
    if proj_lines:
        sections["projects"] = proj_lines

    # Certifications: studio certifications + base certifications
    cert_lines, cert_skills = _format_certifications(certifications)
    extra_skills.extend(cert_skills)
    seen_cert = {line.casefold() for line in cert_lines}
    if base_resume:
        for line in base_resume.section("certifications"):
            cleaned = line.strip()
            if cleaned and cleaned.casefold() not in seen_cert:
                seen_cert.add(cleaned.casefold())
                cert_lines.append(cleaned)
    if cert_lines:
        sections["certifications"] = cert_lines

    base_skills = base_resume.section("skills") if base_resume else []
    merged_skills = _merge_skills(base_skills, extra_skills)
    if merged_skills:
        sections["skills"] = merged_skills

    if base_resume:
        for passthrough in ("education", "publications", "awards", "languages"):
            kept = [line.strip() for line in base_resume.section(passthrough) if line.strip()]
            if kept:
                sections[passthrough] = kept

    draft = edit.rebuild_raw_lines(ParsedResume(contact=contact, sections=sections))
    if job_description.strip():
        draft = edit.rebuild_raw_lines(_with_ordered_skills(draft, job_description))

    rewritten_count = 0
    refused_count = 0
    if use_llm and provider is not None and job_description.strip():
        _, bullets = tailorable_bullets(draft)
        if bullets:
            corpus = SourceCorpus.from_resume(draft, linked=linked_evidence)
            tailor_result = await tailor_bullets(provider, bullets, job_description, corpus)
            rewritten_count = tailor_result.changed_count
            refused_count = tailor_result.rejected
            draft = edit.rebuild_raw_lines(
                apply_rewrites(draft, tailor_result, posting_text=job_description)
            )

    ats_report = ats.score(draft, job_description)
    kw_report = (
        analyze(job_description, SourceCorpus.from_resume(draft))
        if job_description.strip()
        else None
    )
    matched = (
        list(ats_report.supported)
        if ats_report.scored_against_posting
        else list(kw_report.supported if kw_report else [])
    )
    missing = (
        list(ats_report.missing)
        if ats_report.scored_against_posting
        else list(kw_report.missing if kw_report else [])
    )

    preview_html = assemble_html(
        draft,
        None,
        AssemblyOptions(include_projects=False, replace_source_projects=False),
    )

    return StudioSynthesis(
        draft=draft,
        role_track=role_track,
        matched_terms=matched,
        missing_terms=missing,
        ats_parse_score=ats_report.parse,
        ats_keyword_score=ats_report.keywords,
        ats_findings=[f.detail for f in ats_report.findings],
        selected_projects=selected_project_names,
        html_preview=preview_html,
        bullets_rewritten=rewritten_count,
        bullets_refused=refused_count,
    )
