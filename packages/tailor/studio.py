"""Resume Studio: a role-targeted draft of the owner's résumé for one pasted posting.

Built 2026-11-14 at the owner's asking. Everything on the draft is one of:

- **The base résumé**, as uploaded.
- **What the owner confirmed on the dashboard**: internships and
  certifications (`studio_sources.py`).
- **GitHub projects**, printed by the existing generated Projects section
  (`projects.render_section_html`): name, description, language and topics as
  GitHub reports them, never a sentence about them. A private repository is
  printed only once the owner includes or pins it (`select.is_listed`), and
  never with its address.
- **A targeted revision** of a bullet that can truthfully gain a posting
  term (`targeted.revise`), held to `rewrite.vet` and the rest of the
  targeted checks. Only those bullets reach a model, and only when asked.

The role preset never reaches the page. Its words help rank projects for a
short posting, nothing more: a headline written from a preset would be a claim
about the owner that nothing they wrote supports (CLAUDE.md §2.1).

A draft is only a preview. Saving it is `saved_lines_check`, then the router.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from packages.core.models import Project
from packages.github.select import is_eligible, score, select_projects
from packages.llm.provider import LLMProvider
from packages.tailor import ats
from packages.tailor.assemble import AssemblyOptions, assemble_html
from packages.tailor.edit import rebuild_raw_lines
from packages.tailor.evidence import comparable_name, matched_job_terms
from packages.tailor.guard import SourceCorpus, line_key, project_names
from packages.tailor.parse import ParsedResume
from packages.tailor.projects import ProjectEntry
from packages.tailor.publish import apply_rewrites
from packages.tailor.rewrite import BulletRewrite, TailorResult, vet
from packages.tailor.studio_sources import Source, same_skills
from packages.tailor.targeted import Plan, Revision, classify, revise

#: Stamped beside the résumé's fields on a saved draft, like `publish.MODE_STAMP`.
STUDIO_STAMP = "resume_studio"


class RoleTrack(StrEnum):
    SWE = "swe"
    FULLSTACK = "fullstack"
    DATA_ML = "data_ml"
    AI_AGENTIC = "ai_agentic"
    CLOUD_DEVOPS = "cloud_devops"
    FDE_HARNESS = "fde_harness"


@dataclass(frozen=True)
class RolePreset:
    label: str
    #: Words that rank a project for this kind of role. Never printed.
    focus: tuple[str, ...]


ROLE_PRESETS: Mapping[RoleTrack, RolePreset] = {
    RoleTrack.SWE: RolePreset(
        "Software Engineer",
        ("backend", "api", "service", "testing", "python", "java", "go", "sql", "postgresql"),
    ),
    RoleTrack.FULLSTACK: RolePreset(
        "Full-Stack Engineer",
        ("react", "typescript", "next.js", "node", "frontend", "api", "javascript", "css"),
    ),
    RoleTrack.DATA_ML: RolePreset(
        "Data / ML Engineer",
        ("pandas", "spark", "sql", "pytorch", "scikit-learn", "pipeline", "etl", "model"),
    ),
    RoleTrack.AI_AGENTIC: RolePreset(
        "AI / Agentic AI Engineer",
        ("llm", "agent", "rag", "retrieval", "embedding", "langchain", "mcp", "evaluation"),
    ),
    RoleTrack.CLOUD_DEVOPS: RolePreset(
        "Cloud / DevOps Engineer",
        ("docker", "kubernetes", "terraform", "aws", "gcp", "azure", "ci", "github actions"),
    ),
    RoleTrack.FDE_HARNESS: RolePreset(
        "FDE / Harness Engineer",
        ("integration", "customer", "api", "harness", "evaluation", "agent", "deployment"),
    ),
}


def ranking_text(job_description: str, track: RoleTrack) -> str:
    return f"{job_description}\n{' '.join(ROLE_PRESETS[track].focus)}"


@dataclass(frozen=True)
class ProjectPick:
    project: Project
    score: float
    matched_terms: tuple[str, ...]
    chosen: bool
    #: Why it is not printed: `evidence only`, `excluded`, `on the résumé`, ``.
    held_back: str = ""


def pick_projects(
    projects: Sequence[Project],
    base: ParsedResume,
    job_description: str,
    track: RoleTrack,
    *,
    selected_ids: Sequence[str] | None = None,
    limit: int = 4,
) -> list[ProjectPick]:
    """Rank every project; choose the owner's selection, or the best eligible ones."""
    on_resume = {comparable_name(name) for name in project_names(base)}
    text = ranking_text(job_description, track)

    def held_back(project: Project) -> str:
        if comparable_name(project.name) in on_resume:
            return "on the résumé"
        if project.include is False:
            return "excluded"
        if not project.resume_listed:
            return "evidence only"
        return ""

    usable = [p for p in projects if not held_back(p)]
    if selected_ids is not None:
        wanted = [str(i) for i in selected_ids]
        by_id = {str(p.id): p for p in usable if is_eligible(p, require_description=False)}
        chosen = [by_id[i] for i in wanted if i in by_id][:limit]
    else:
        chosen = select_projects(usable, text, limit=limit)
    chosen_ids = {p.id for p in chosen}
    ranked = sorted(projects, key=lambda p: score(p, text), reverse=True)
    return [
        ProjectPick(
            project=project,
            score=round(score(project, text), 4),
            matched_terms=tuple(matched_job_terms(project, job_description)),
            chosen=project.id in chosen_ids,
            held_back=held_back(project),
        )
        for project in ranked
    ]


@dataclass(frozen=True)
class BulletTrace:
    """One tailorable bullet: what was sent, what came back, and on what evidence."""

    ref: str
    original: str
    tailored: str
    #: `kept`, `rewritten`, `refused`, `provider failed`, or `not asked`.
    status: str
    reason: str | None = None
    #: (term, evidence, kind) per target. Kind is `bullet`, `practice` or `repository`.
    targets: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class Draft:
    source: Source
    resume: ParsedResume
    projects: tuple[Project, ...]
    plan: Plan
    bullets: tuple[BulletTrace, ...]
    ats_before: ats.AtsReport
    ats_after: ats.AtsReport
    html: str
    tailored_by: str | None = None
    calls: int = 0
    picks: tuple[ProjectPick, ...] = field(default_factory=tuple)


def _status(rewrite: BulletRewrite, asked: bool) -> str:
    if rewrite.provider_failed:
        return "provider failed"
    if rewrite.rejected_reason:
        return "refused"
    if rewrite.changed:
        return "rewritten"
    return "kept" if asked else "not asked"


def _traces(
    plan: Plan, result: TailorResult, corpus: SourceCorpus, asked: bool
) -> list[BulletTrace]:
    traces = []
    for index, rewrite in enumerate(result.bullets):
        targets = plan.targets.get(index, ())
        item = corpus.locate(rewrite.original)
        traces.append(
            BulletTrace(
                ref=item.ref if item else "",
                original=rewrite.original,
                tailored=rewrite.tailored,
                status=_status(rewrite, asked and bool(targets)),
                reason=rewrite.rejected_reason,
                targets=tuple(
                    (
                        target.term,
                        target.evidence,
                        "repository"
                        if target.from_repository
                        else "practice"
                        if target.practice
                        else "bullet",
                    )
                    for target in targets
                ),
            )
        )
    return traces


def project_lines(project: Project) -> list[str]:
    """How a generated project reads as text: for the ATS score and the DOCX."""
    entry = ProjectEntry.from_project(project)
    title = f"{entry.name} — {entry.description}" if entry.description else entry.name
    lines = [title]
    if entry.stack:
        lines.append(", ".join(entry.stack))
    if not entry.is_private:
        lines.append(entry.slug)
    return lines


def as_printed(resume: ParsedResume, projects: Sequence[Project] = ()) -> ParsedResume:
    """`resume` with the generated projects in its text, for scoring as it would read."""
    printed = resume.model_copy(deep=True)
    if projects:
        generated = [line for project in projects for line in project_lines(project)]
        printed.sections["projects"] = [*generated, *printed.section("projects")]
    return rebuild_raw_lines(printed)


def preview_options(projects: Sequence[Project]) -> AssemblyOptions:
    """The generated section beside the résumé's own Projects, never instead of it."""
    return AssemblyOptions(include_projects=bool(projects), replace_source_projects=False)


async def synthesize(
    base: ParsedResume,
    source: Source,
    job_description: str,
    track: RoleTrack,
    *,
    projects: Sequence[Project] = (),
    linked: Mapping[str, str] | None = None,
    selected_project_ids: Sequence[str] | None = None,
    project_limit: int = 4,
    provider: LLMProvider | None = None,
) -> Draft:
    """A draft for one posting. Calls `provider` only for bullets that can gain a term."""
    picks = pick_projects(
        projects,
        base,
        job_description,
        track,
        selected_ids=selected_project_ids,
        limit=project_limit,
    )
    chosen = [pick.project for pick in picks if pick.chosen]
    corpus = SourceCorpus.from_resume(source.resume, linked=linked, verified=source.verified)

    revision: Revision | None = None
    if provider is not None:
        revision = await revise(
            provider,
            source.resume,
            job_description,
            projects=chosen,
            linked=linked,
            verified=source.verified,
        )
        plan, result = revision.plan, revision.result
    else:
        plan = classify(
            source.resume,
            job_description,
            projects=chosen,
            linked=linked,
            verified=source.verified,
        )
        result = TailorResult(bullets=[BulletRewrite(original=b, tailored=b) for b in plan.bullets])

    resume = apply_rewrites(source.resume, result, posting_text=job_description)
    calls = revision.calls if revision else 0
    answered = getattr(provider, "answered_by", None) or getattr(provider, "name", None)
    return Draft(
        source=source,
        resume=resume,
        projects=tuple(chosen),
        plan=plan,
        bullets=tuple(_traces(plan, result, corpus, provider is not None)),
        ats_before=ats.score(as_printed(base), job_description),
        ats_after=ats.score(as_printed(resume, chosen), job_description),
        html=assemble_html(resume, chosen, preview_options(chosen)),
        tailored_by=str(answered) if calls and answered else None,
        calls=calls,
        picks=tuple(picks),
    )


def saved_lines_check(
    source: Source,
    sections: Mapping[str, Sequence[str]],
    *,
    rewrites: Sequence[tuple[str, str]] = (),
    owner_edits: Sequence[str] = (),
    linked: Mapping[str, str] | None = None,
) -> list[str]:
    """Lines of a draft about to be saved that nothing backs. Empty means it may be saved.

    Every line must be the source's, a rewrite of a source line that passes
    `rewrite.vet` again here, or one the owner typed. The draft comes back
    from the browser, so a rewrite is judged again rather than trusted, and a
    line that is none of the three is refused rather than saved.
    """
    corpus = SourceCorpus.from_resume(source.resume, linked=linked, verified=source.verified)
    source_keys = {line_key(line) for lines in source.resume.sections.values() for line in lines}
    skills = source.resume.section("skills")
    allowed = source_keys | {line_key(edit) for edit in owner_edits}
    for original, tailored in rewrites:
        if line_key(original) in source_keys and vet(original, tailored, corpus)[0]:
            allowed.add(line_key(tailored))

    def backed(section: str, line: str) -> bool:
        key = line_key(line)
        if not key or key in allowed:
            return True
        return section == "skills" and any(same_skills(line, s) for s in skills)

    return [line for name, lines in sections.items() for line in lines if not backed(name, line)]
