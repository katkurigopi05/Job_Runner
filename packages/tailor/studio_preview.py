"""Assemble a Resume Studio preview from a synthesized draft and verified GitHub evidence.

Shared by `apps/api/routers/resume_studio.py` and the opt-in LangGraph pilot
(`packages/tailor/studio_pilot.py`) so both build identical previews without
importing FastAPI route code into `packages/`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from packages.core.models import Project, Resume
from packages.core.schemas import ResumeContactEdit
from packages.core.schemas_studio import (
    StudioAts,
    StudioBullet,
    StudioLine,
    StudioPreview,
    StudioProject,
    StudioTarget,
    StudioTerm,
)
from packages.github.code_search import SearchResult
from packages.llm.provider import LLMProvider
from packages.tailor.ats import AtsReport
from packages.tailor.code_evidence import review_evidence
from packages.tailor.guard import line_key
from packages.tailor.parse import ParsedResume
from packages.tailor.studio import ROLE_PRESETS, Draft, RoleTrack, synthesize
from packages.tailor.studio_sources import Source


def _ats(report: AtsReport) -> StudioAts:
    return StudioAts(
        parse=report.parse,
        keywords=report.keywords,
        overall=report.overall,
        supported=list(report.supported),
        missing=list(report.missing),
        findings=[str(finding) for finding in report.findings],
    )


def _lines(draft: Draft) -> dict[str, list[StudioLine]]:
    """Every draft line with where it came from. A rewrite names the line it replaced."""
    rewritten = {line_key(b.tailored): b.original for b in draft.bullets if b.status == "rewritten"}
    out: dict[str, list[StudioLine]] = {}
    for section, lines in draft.resume.sections.items():
        rows = []
        for text in lines:
            original = rewritten.get(line_key(text))
            if original is not None:
                rows.append(StudioLine(text=text, origin="rewrite", original=original))
                continue
            origin = draft.source.origin_of(text)
            rows.append(StudioLine(text=text, origin=origin.kind, label=origin.label))
        out[section] = rows
    return out


def preview_of(draft: Draft, base: Resume, track: RoleTrack) -> StudioPreview:
    rewrites = {item.project_id: item for item in draft.project_rewrites}
    return StudioPreview(
        role_track=track.value,
        role_label=ROLE_PRESETS[track].label,
        base_resume_id=base.id,
        base_version=base.version,
        contact=ResumeContactEdit.model_validate(draft.resume.contact.model_dump()),
        sections={name: list(lines) for name, lines in draft.resume.sections.items()},
        lines=_lines(draft),
        html=draft.html,
        bullets=[
            StudioBullet(
                ref=b.ref,
                original=b.original,
                tailored=b.tailored,
                status=b.status,
                reason=b.reason,
                targets=[StudioTarget(term=t, evidence=e, kind=k) for t, e, k in b.targets],
            )
            for b in draft.bullets
        ],
        terms=[
            StudioTerm(term=t.term, status=t.status.value, sources=list(t.sources))
            for t in draft.plan.terms
        ],
        projects=[
            StudioProject(
                id=pick.project.id,
                name=pick.project.name,
                is_private=pick.project.is_private,
                score=pick.score,
                matched_terms=list(pick.matched_terms),
                chosen=pick.chosen,
                held_back=pick.held_back,
                original_description=pick.project.description or "",
                tailored_description=(
                    rewrites[pick.project.id].tailored
                    if pick.project.id in rewrites
                    else pick.project.description or ""
                ),
                rewrite_status=(
                    rewrites[pick.project.id].status if pick.project.id in rewrites else "not asked"
                ),
                rewrite_reason=(
                    rewrites[pick.project.id].reason if pick.project.id in rewrites else None
                ),
            )
            for pick in draft.picks
        ],
        ats_before=_ats(draft.ats_before),
        ats_after=_ats(draft.ats_after),
        added=list(draft.source.added),
        skipped=list(draft.source.skipped),
        tailored_by=draft.tailored_by,
        calls=draft.calls,
    )


async def assemble_live_code_preview(
    *,
    base_row: Resume,
    base: ParsedResume,
    source: Source,
    job_description: str,
    track: RoleTrack,
    projects: Sequence[Project],
    code: SearchResult,
    selected_project_ids: Sequence[uuid.UUID] | None = None,
    project_limit: int = 4,
    provider: LLMProvider | None = None,
) -> StudioPreview:
    """Build the exact live-code StudioPreview from a verified SearchResult."""
    draft = await synthesize(
        base,
        source,
        job_description,
        track,
        projects=projects,
        linked=None,
        selected_project_ids=(
            None if selected_project_ids is None else [str(i) for i in selected_project_ids]
        ),
        project_limit=project_limit,
        provider=provider,
        replace_source_projects=False,
        declared=None,
        code_verified=code.implemented(),
    )
    preview = preview_of(draft, base_row, track)
    preview.project_evidence = f"Live code evidence checked {code.searched} GitHub repositories."
    preview.code_review = review_evidence(
        job_description,
        source.resume,
        code,
        projects=projects,
        chosen={project.id for project in draft.projects},
        draft_resume=draft.resume,
    )
    return preview
