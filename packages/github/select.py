"""Choose which projects go on a résumé, and in what order.

Deliberately deterministic — no model call. Two reasons: the owner runs their
own LLM and this should work without it, and a ranking you cannot predict is
one you cannot trust to be stable across every résumé you send.

The ordering rule: pinned first, then by score. Score rewards recency, then
relevance to the posting, then modest signals of substance. Stars are
intentionally a weak signal; a well-matched project with two stars should beat
an unrelated one with two hundred.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from packages.core.models import Project
from packages.matching.embed import Embedder, cosine
from packages.tailor.keywords import job_terms

#: How many projects a résumé section should carry by default. More than this
#: and it stops being a highlight reel.
DEFAULT_LIMIT = 4

#: A project that has not been touched in this long is probably not the best
#: thing to lead with.
STALE_AFTER_DAYS = 365 * 3

_WORD_RE = re.compile(r"[a-z0-9+#.]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def is_eligible(project: Project, *, require_description: bool = True) -> bool:
    """Whether a project may appear at all.

    An excluded project is never eligible; an explicitly included one always
    is. Otherwise: no forks, no archived repos, and — by default — nothing
    without a description, because the alternative is either a bare repo name
    or an invented summary, and §2.1 rules out the second.
    """
    if project.include is False:
        return False
    if project.include is True:
        return True
    if project.is_fork or project.is_archived:
        return False
    return not (require_description and not (project.description or "").strip())


def relevance(project: Project, job_text: str) -> float:
    """Overlap between the project's own words and the posting's. 0.0–1.0.

    Only uses text the project actually carries — name, description, language,
    topics. Nothing is inferred about what the project does.
    """
    if not job_text.strip():
        return 0.0

    haystack = " ".join(
        filter(
            None,
            [
                project.name,
                project.description or "",
                project.language or "",
                " ".join(project.topics_json or []),
            ],
        )
    )
    project_tokens = _tokens(haystack)
    if not project_tokens:
        return 0.0

    job_tokens = _tokens(job_text)
    overlap = project_tokens & job_tokens
    return len(overlap) / len(project_tokens)


def recency(project: Project, *, now: datetime | None = None) -> float:
    """1.0 for pushed today, decaying to 0.0 at STALE_AFTER_DAYS."""
    if project.pushed_at is None:
        return 0.0
    current = now or datetime.now(UTC)
    pushed = project.pushed_at
    if pushed.tzinfo is None:
        pushed = pushed.replace(tzinfo=UTC)
    age_days = (current - pushed).days
    if age_days <= 0:
        return 1.0
    if age_days >= STALE_AFTER_DAYS:
        return 0.0
    return 1.0 - (age_days / STALE_AFTER_DAYS)


def substance(project: Project) -> float:
    """A weak signal that a project is more than a scratch repo. 0.0–1.0."""
    score = 0.0
    if (project.description or "").strip():
        score += 0.4
    if project.topics_json:
        score += 0.2
    if project.homepage:
        score += 0.2
    # Stars are capped low on purpose — popularity is not relevance.
    score += min(project.stars, 50) / 50 * 0.2
    return min(score, 1.0)


def score(project: Project, job_text: str = "", *, now: datetime | None = None) -> float:
    """Weighted rank. Relevance dominates once a posting is known."""
    if job_text.strip():
        return (
            0.50 * relevance(project, job_text)
            + 0.30 * recency(project, now=now)
            + 0.20 * substance(project)
        )
    # No posting to match against: lead with what is recent and substantial.
    return 0.60 * recency(project, now=now) + 0.40 * substance(project)


def select_projects(
    projects: list[Project],
    job_text: str = "",
    *,
    limit: int = DEFAULT_LIMIT,
    require_description: bool = True,
    now: datetime | None = None,
) -> list[Project]:
    """The projects to put on one résumé, best first.

    Pinned projects come first in their own ranked order and always make the
    cut, so the owner can guarantee a project appears on every résumé.
    """
    eligible = [p for p in projects if is_eligible(p, require_description=require_description)]

    pinned = [p for p in eligible if p.pinned]
    rest = [p for p in eligible if not p.pinned]

    pinned.sort(key=lambda p: score(p, job_text, now=now), reverse=True)
    rest.sort(key=lambda p: score(p, job_text, now=now), reverse=True)

    return (pinned + rest)[:limit]


#: Cosine above which a project counts as related to a posting despite sharing
#: none of its words. Measured, not chosen: against eight hand-written cases
#: the related ones scored 0.573-0.887 and the unrelated 0.451-0.482, so 0.53
#: sits in a 0.091 gap.
#:
#: That gap only exists because the comparison is against the posting's
#: *extracted terms* rather than its full text. Against the whole description
#: the same eight cases separated by 0.019 — a Jekyll blog at 0.523 against a
#: Prometheus dashboard at 0.542 — which is not a threshold, it is a
#: coincidence. Short project text against a long description embeds mostly as
#: "is technical".
#:
#: Eight synthetic cases is a calibration, not evidence. Recheck it against
#: real repositories and real postings before trusting it.
SEMANTIC_THRESHOLD = 0.53


def _semantic_relatedness(project: Project, terms_vector: list[float], embedder: Embedder) -> float:
    """Cosine between the project's own words and the posting's salient terms."""
    text = " ".join(
        filter(
            None,
            [
                project.name,
                project.description or "",
                project.language or "",
                " ".join(project.topics_json or []),
            ],
        )
    )
    if not text.strip():
        return 0.0
    return cosine(terms_vector, embedder.encode([text])[0])


def relevant_for_posting(
    projects: list[Project],
    job_text: str,
    *,
    limit: int = DEFAULT_LIMIT,
    now: datetime | None = None,
    embedder: Embedder | None = None,
    threshold: float = SEMANTIC_THRESHOLD,
) -> list[Project]:
    """The projects worth putting on a résumé tailored to *this* posting.

    `select_projects` ranks and then fills up to `limit`, which is right for a
    general résumé and wrong for a targeted one: with a thin inventory it puts
    an unrelated repository on the page purely because there was room. On a
    résumé aimed at one job, a project that evidences nothing about that job
    is worse than a shorter section — it spends the reader's attention and
    says nothing.

    So ranking still decides the order, and a project must additionally either
    be pinned or share vocabulary with the posting. Pinning is the owner's
    explicit "always show this" and outranks relevance, exactly as
    `is_eligible` already treats `include`.

    Only source-reported text is consulted — `relevance` reads name,
    description, language and topics, all copied from GitHub. Nothing is
    inferred about what a project does, which is what keeps a Projects section
    inside §2.1.
    """
    ranked = select_projects(projects, job_text, limit=limit, now=now)

    kept = [p for p in ranked if p.pinned or relevance(p, job_text) > 0]
    if embedder is None or not job_text.strip():
        return kept

    # Shared vocabulary is precise and narrow. It keeps "Kubernetes clusters"
    # against a Kubernetes posting and drops "k8s homelab", "Docker Swarm" and
    # "Terraform modules" against the same one — the last of which the posting
    # asked for by name as "infrastructure as code". A résumé that omits the
    # owner's Terraform work because the employer spelled it differently is
    # the failure this second pass exists for.
    undecided = [p for p in ranked if p not in kept]
    if not undecided:
        return kept

    terms = job_terms(job_text)
    if not terms:
        return kept
    terms_vector = embedder.encode([" ".join(terms)])[0]

    related = {
        id(p) for p in undecided if _semantic_relatedness(p, terms_vector, embedder) >= threshold
    }
    # Rebuilt from `ranked` rather than appended, so the ordering `select_projects`
    # decided survives the second pass.
    return [p for p in ranked if p in kept or id(p) in related]
