"""Verified GitHub evidence that may appear in a tailored résumé.

Repository names, descriptions, primary languages, and topics are copied from
GitHub by the existing sync. They are useful evidence for a skill the source
résumé omitted, but only when they remain attributed to that project. This
module never reads an employer bullet and never generates a claim.
"""

from __future__ import annotations

from packages.core.models import Project
from packages.tailor.guard import SourceCorpus
from packages.tailor.keywords import analyze


def project_source_text(project: Project) -> str:
    """Flatten only source-reported project fields for matching and audit."""
    return "\n".join(
        part
        for part in (
            project.name,
            project.description or "",
            project.language or "",
            "\n".join(project.topics_json or []),
        )
        if part.strip()
    )


def matched_job_terms(project: Project, job_description: str) -> list[str]:
    """Salient posting terms directly supported by one GitHub project."""
    if not job_description.strip():
        return []
    evidence = SourceCorpus.from_texts(project_source_text(project))
    return analyze(job_description, evidence).supported
