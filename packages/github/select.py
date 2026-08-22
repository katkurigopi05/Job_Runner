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


def relevant_for_posting(
    projects: list[Project],
    job_text: str,
    *,
    limit: int = DEFAULT_LIMIT,
    now: datetime | None = None,
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
    return [p for p in ranked if p.pinned or relevance(p, job_text) > 0]
