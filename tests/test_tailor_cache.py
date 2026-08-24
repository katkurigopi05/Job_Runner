"""Reusing a tailored résumé instead of buying it twice.

CLAUDE.md §15 named this gap and said when it would start mattering: the moment
tailoring runs on a remote provider. It does — `LLM_PROVIDER=gemini`, and the
audit trail holds 308 uploads and 1,005,518 characters, with 204 uploads on one
day against a ceiling of 200 that §7 has *refuse* rather than downgrade.

Most of these tests are about the key, because the key is the whole design. A
cache that returns the wrong résumé is worse than no cache: the document is
plausible, it is addressed to the right person, and nothing about it looks
wrong — it was simply written for a different job.
"""

from __future__ import annotations

import uuid

from packages.core.models import Candidate, Project, Resume, User
from packages.tailor.cache import find_cached, tailoring_key

RESUME = {"raw_lines": ["Built backend services in Python."]}


def _project(name: str = "repo") -> Project:
    return Project(id=uuid.uuid4(), candidate_id=uuid.uuid4(), source="github", name=name, url="u")


def _key(**over):
    base = dict(
        source_resume_id=uuid.UUID(int=1),
        content_hash="hash-a",
        projects=[],
        provider="gemini",
        model="gemini-2.0",
    )
    base.update(over)
    return tailoring_key(**base)


# --- the key ---------------------------------------------------------------


def test_the_same_inputs_produce_the_same_key() -> None:
    """Without this there is no cache, only a second copy of everything."""
    assert _key() == _key()


def test_an_edited_posting_is_a_different_key() -> None:
    """The failure that matters: serving a résumé written for another job."""
    assert _key() != _key(content_hash="hash-b")


def test_a_different_source_resume_is_a_different_key() -> None:
    """Uploading a new résumé must not keep sending the old one's tailoring."""
    assert _key() != _key(source_resume_id=uuid.UUID(int=2))


def test_a_different_model_is_a_different_key() -> None:
    """Same inputs through another model is not the same document."""
    assert _key() != _key(model="gemini-3.0")
    assert _key() != _key(provider="ollama")


def test_attaching_a_project_is_a_different_key() -> None:
    """A GitHub sync that adds a repository changes what gets rendered."""
    assert _key() != _key(projects=[_project()])


def test_project_order_does_not_change_the_key() -> None:
    """Ranking is not part of identity; the set of projects is."""
    a, b = _project("one"), _project("two")
    assert _key(projects=[a, b]) == _key(projects=[b, a])


def test_editing_the_prompt_invalidates_every_entry(monkeypatch) -> None:
    """Nobody has to remember to clear the cache after rewriting the prompt.

    §2.1 lives in that prompt. A rewrite of it is exactly the moment stale
    tailorings must stop being served.
    """
    from packages.llm.prompts import Prompt

    before = _key()
    monkeypatch.setattr(
        "packages.tailor.cache.TAILOR_SYSTEM",
        Prompt(name="tailor.system", version=3, text="different text entirely"),
    )
    assert _key() != before


def test_a_posting_with_no_content_hash_is_not_cacheable() -> None:
    """None, not a key made up from something weaker.

    `content_hash` is what makes two postings the same posting. Substituting
    the URL or the title would let an edited description serve the old résumé.
    """
    assert _key(content_hash="") is None
    assert _key(content_hash="   ") is None
    assert _key(content_hash=None) is None


# --- the lookup ------------------------------------------------------------


async def _owner(session) -> tuple[Candidate, Resume]:
    suffix = uuid.uuid4().hex[:8]
    user = User(email=f"c-{suffix}@example.com")
    session.add(user)
    await session.flush()
    candidate = Candidate(user_id=user.id, name="Owner", email=f"c-{suffix}@example.com")
    session.add(candidate)
    await session.flush()
    resume = Resume(
        candidate_id=candidate.id, version=1, storage_ref=f"r/{suffix}", parsed_json=RESUME
    )
    session.add(resume)
    await session.flush()
    return candidate, resume


async def test_an_uncacheable_tailoring_is_never_looked_up(db_session) -> None:
    """A None key must not degrade into "match anything"."""
    candidate, _ = await _owner(db_session)

    assert await find_cached(db_session, candidate_id=candidate.id, key=None) is None


async def test_a_stored_key_is_found_again(db_session) -> None:
    candidate, _ = await _owner(db_session)
    tailored = Resume(
        candidate_id=candidate.id,
        version=2,
        storage_ref="r/tailored",
        parsed_json=RESUME,
        tailored_key="k1",
    )
    db_session.add(tailored)
    await db_session.flush()

    found = await find_cached(db_session, candidate_id=candidate.id, key="k1")

    assert found is not None and found.id == tailored.id


async def test_a_base_resume_is_never_served_as_a_tailoring(db_session) -> None:
    """Uploaded résumés carry a NULL key, and NULL matches nothing."""
    candidate, base = await _owner(db_session)

    assert base.tailored_key is None
    assert await find_cached(db_session, candidate_id=candidate.id, key="k1") is None


async def test_the_cache_does_not_reach_across_candidates(db_session) -> None:
    """Impossible by construction rather than by argument — see find_cached."""
    mine, _ = await _owner(db_session)
    theirs, _ = await _owner(db_session)
    db_session.add(
        Resume(
            candidate_id=theirs.id,
            version=2,
            storage_ref="r/theirs",
            parsed_json=RESUME,
            tailored_key="shared",
        )
    )
    await db_session.flush()

    assert await find_cached(db_session, candidate_id=mine.id, key="shared") is None
