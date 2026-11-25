"""Studio's implementation findings, kept with the draft whose save verified them.

A live code check in Resume Studio reads a repository at a commit and finds
where its code implements a skill (`code_search.CodeFinding`). Those findings
lived only inside one preview, so a skill-gap answer could say no more than
that a repository *declares* a technology. A save that keeps an approved
claim checks the code again on the server (`routers/resume_studio.save_draft`);
`kept` turns that check's findings into what the saved draft stores, and
`implemented` reads them back for the gap drill-down (`gap_evidence.py`).

**Repository facts, never the owner's.** A finding says a file at a commit
implements the skill, not who wrote it. Contribution is still confirmed claim by
claim in Studio (`StudioCodeApproval.contribution_confirmed`).

**What is kept**: a public repository's finding that can support a claim
(implemented, not contradicted by checked-in documentation) with a permalink
pinned to its own commit. Its path, lines, commit, link and the rule's fixed
explanation: never source text and never the suggested wording.

Light on purpose: the graph reads this, and Studio's own module pulls in the
model providers.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Annotated, Any

import structlog
from pydantic import ConfigDict, Field, StringConstraints, ValidationError

from packages.core.models import Resume
from packages.core.schemas_graph import CodeCitationOut

if TYPE_CHECKING:
    from packages.github.code_search import CodeFinding

log = structlog.get_logger(__name__)

#: Stamped beside the résumé's fields on a saved draft, like `publish.MODE_STAMP`.
STUDIO_STAMP = "resume_studio"
#: Under `STUDIO_STAMP`: the findings of the check the save ran.
FINDINGS_KEY = "github_code_findings"
#: Files a gap cites per project and skill.
CITATIONS_PER_SKILL = 3


class KeptFinding(CodeCitationOut):
    """A citation (file, lines, commit, link) and the skills it implements, as a save keeps it.

    The pin to its commit is `CodeCitationOut`'s rule, so what is kept is
    already what an answer may cite.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    project_id: uuid.UUID
    skills: tuple[Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]+$")], ...] = Field(
        min_length=1
    )

    def citation(self) -> CodeCitationOut:
        return CodeCitationOut.model_validate(
            self.model_dump(include=set(CodeCitationOut.model_fields))
        )


def kept(findings: Sequence[CodeFinding]) -> list[dict[str, Any]]:
    """What a save stores of its check: public, claim-supporting, commit-pinned findings."""
    saved: list[dict[str, Any]] = []
    for finding in findings:
        if not finding.can_support_claim or finding.is_private or not finding.permalink:
            continue
        try:
            entry = KeptFinding(
                id=finding.id,
                project_id=finding.project_id,
                skills=finding.skills,
                path=finding.path,
                start_line=finding.start_line,
                end_line=finding.end_line,
                commit_sha=finding.commit_sha,
                url=finding.permalink,
                explanation=finding.explanation,
            )
        except ValidationError as exc:
            # A check's own finding that is not pinned to its commit is a bug
            # upstream; keeping it would cite a file the answer cannot stand behind.
            log.warning("studio_finding_not_kept", finding_id=finding.id, errors=exc.error_count())
            continue
        saved.append(entry.model_dump(mode="json"))
    return saved


def _entries(resume: Resume) -> list[object]:
    stamp = (resume.parsed_json or {}).get(STUDIO_STAMP)
    if not isinstance(stamp, Mapping):
        return []
    entries = stamp.get(FINDINGS_KEY)
    return list(entries) if isinstance(entries, list) else []


def implemented(resumes: Iterable[Resume]) -> dict[tuple[uuid.UUID, str], tuple[KeptFinding, ...]]:
    """Every saved draft's kept findings, by (project, skill), each finding once.

    An entry that no longer validates is skipped and logged, never trusted: it
    would be cited as implementation.
    """
    by_id: dict[str, KeptFinding] = {}
    skipped = 0
    for resume in resumes:
        for entry in _entries(resume):
            try:
                finding = KeptFinding.model_validate(entry)
            except ValidationError:
                skipped += 1
                continue
            by_id.setdefault(finding.id, finding)
    if skipped:
        log.warning("studio_findings_skipped", count=skipped)
    cited: dict[tuple[uuid.UUID, str], list[KeptFinding]] = defaultdict(list)
    for finding in by_id.values():
        for skill in finding.skills:
            cited[(finding.project_id, skill)].append(finding)
    return {
        key: tuple(sorted(found, key=lambda f: (f.path, f.start_line))[:CITATIONS_PER_SKILL])
        for key, found in cited.items()
    }
