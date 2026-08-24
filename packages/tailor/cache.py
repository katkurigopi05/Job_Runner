"""Reuse a tailored résumé instead of paying for it twice.

CLAUDE.md §15 recorded this gap and predicted when it would matter: "Every
apply re-tailors from scratch. Harmless today because tailoring is cheap and
local; it becomes a cost the moment a remote provider is used for it." That
moment has arrived — `LLM_PROVIDER=gemini` with a key set, and the audit trail
holds 308 uploads totalling 1,005,518 characters. On 2026-08-21 it recorded 204
uploads against a ceiling of 200, and §7 has the quota *refuse* rather than
downgrade, so re-tailoring does not merely cost money: past the ceiling it
stops tailoring working at all.

The cost that matters most is not the money. Every re-tailor re-uploads résumé
text to a third party, and §2.8 permits that upload while asking that it be
auditable and minimal. A cache hit sends nothing.

## The key is the whole design

A cache keyed on too little is worse than no cache: it serves a résumé tailored
for a different posting, or one written by a prompt that has since been
rewritten, and nothing looks wrong. So the key covers everything that changes
the output —

- **the source résumé**, by id — different base, different bullets;
- **the posting**, by `content_hash`, which `posting_hash` computes over
  external id, title, location and description, so an edited description is a
  different key;
- **the prompt**, by `TAILOR_SYSTEM.digest` — editing the prompt invalidates
  every entry without anyone remembering to;
- **the projects attached**, by id — `packages/tailor/evidence.py` chooses
  these per posting, and a GitHub sync that adds a repository changes the
  document;
- **the provider and model**, because the same inputs through a different model
  are not the same résumé.

Anything omitted here becomes a wrong answer served confidently. Anything added
that does not affect output only costs a miss, which is why the key leans
toward over-specifying.
"""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Project, Resume
from packages.llm.prompts import TAILOR_SYSTEM

#: Separator that cannot occur in an id, a hash, or a model name, so two
#: different component lists cannot flatten to the same string.
_SEP = "\x1f"


def tailoring_key(
    *,
    source_resume_id: uuid.UUID,
    content_hash: str | None,
    projects: list[Project] | None,
    provider: str,
    model: str | None,
) -> str | None:
    """The cache key for one tailoring, or None when it must not be cached.

    Takes the hash rather than a posting on purpose. Two different things in
    this codebase are called `posting`: the `Posting` row, which has a
    `content_hash`, and `ats.base.ParsedPosting`, which the adapter reads off
    the page and which has no such field. A parameter typed for one silently
    accepts the other at runtime — asking for the hash makes the caller resolve
    that question where it can be answered.

    None rather than a fabricated key when there is no hash: it is what makes
    two postings the same posting, and without it the only honest options are
    to key on something weaker or not to cache. Guessing serves one posting's
    résumé for another.
    """
    content_hash = (content_hash or "").strip()
    if not content_hash:
        return None

    project_ids = sorted(str(project.id) for project in (projects or []))
    material = _SEP.join(
        [
            str(source_resume_id),
            content_hash,
            TAILOR_SYSTEM.digest,
            provider,
            model or "",
            ",".join(project_ids),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


async def find_cached(
    session: AsyncSession, *, candidate_id: uuid.UUID, key: str | None
) -> Resume | None:
    """A résumé already tailored for exactly this, if one exists.

    Scoped to the candidate as well as the key. The key already contains the
    source résumé's id, so a collision across people is not reachable — but a
    cache that reads another candidate's rows is the kind of thing that should
    be impossible by construction rather than by argument, and §1 has one owner
    only because that is today's shape, not a guarantee.

    Newest first: a re-render after a template change writes a second row with
    the same key, and the later one is the one that matches what would be
    produced now.
    """
    if key is None:
        return None
    return (
        await session.scalars(
            select(Resume)
            .where(
                Resume.candidate_id == candidate_id,
                Resume.tailored_key == key,
            )
            .order_by(Resume.created_at.desc())
            .limit(1)
        )
    ).first()
