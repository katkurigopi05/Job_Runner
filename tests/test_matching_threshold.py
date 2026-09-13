"""What `MIN_DOCUMENTS = 50` actually does, settled by test rather than by reading.

An earlier review said two incompatible things about it — that matching is
"gated" by it, and that early matches "run on fallback weighting". Only one can
be true and the difference decides whether a small corpus produces a feed at
all.

**It does not block matching.** It switches the *weighting*:

    frequencies.usable  -> LexicalEmbedder(frequencies=...)  # "lexical-idf@2"
    not usable          -> get_embedder()                    # "lexical@2"

and the gap report falls back to the hand-written stopword list that IDF exists
to replace. So a cold start scores everything, with weights that describe the
domain less well — and `embedding_model` on the row records which, so the two
are never silently mixed.

The safeguard is real and is not lowered here: below 50 documents a term seen in
three of them looks rare because the sample is small, not because it is.
"""

from __future__ import annotations

from sqlalchemy import select

from packages.core.models import Candidate, Match, Posting, Profile, Project, User
from packages.matching.idf import MIN_DOCUMENTS, DocumentFrequencies, refresh_corpus_stats
from packages.matching.incremental import _embedder_for, run_matching_pass


async def _profile(db_session, label: str = "primary") -> Profile:
    user = User(email=f"{label}@example.com")
    db_session.add(user)
    await db_session.flush()
    candidate = Candidate(
        user_id=user.id, name="Owner", email=f"{label}@example.com", email_mode="self"
    )
    db_session.add(candidate)
    await db_session.flush()
    profile = Profile(candidate_id=candidate.id, label=label)
    db_session.add(profile)
    db_session.add(
        Project(
            candidate_id=candidate.id,
            source="github",
            external_id=f"{label}-1",
            name="telemetry",
            full_name=f"owner/{label}",
            url=f"https://github.com/owner/{label}",
            description="python django postgres telemetry ingestion service",
            language="Python",
            topics_json=["python", "django", "postgres"],
        )
    )
    await db_session.flush()
    return profile


async def _postings(db_session, count: int) -> None:
    db_session.add_all(
        [
            Posting(
                url=f"https://example.com/p/{n}",
                external_id=f"p-{n}",
                title=f"Backend Engineer {n}",
                description_raw="python django postgres service team benefits",
            )
            for n in range(count)
        ]
    )
    await db_session.flush()


def test_the_threshold_is_about_the_sample_not_a_gate() -> None:
    """`usable` is a statement about the statistic, not permission to score."""
    small = DocumentFrequencies.from_texts([f"python role {n}" for n in range(MIN_DOCUMENTS - 1)])
    big = DocumentFrequencies.from_texts([f"python role {n}" for n in range(MIN_DOCUMENTS)])

    assert small.usable is False
    assert big.usable is True
    # Both still answer every question asked of them — nothing raises or refuses.
    assert small.idf("python") > 0
    assert small.document_share("python") == 1.0


async def test_below_the_threshold_matching_still_runs(db_session) -> None:
    """The cold start. Ten postings, no corpus statistics, and a real feed."""
    profile = await _profile(db_session)
    await _postings(db_session, 10)

    report = await run_matching_pass(db_session)

    assert report.revision is None, "no corpus statistics were built"
    assert report.rebuilt is False
    assert report.embedded == 10, "every posting was embedded anyway"
    assert report.scored == 10, "and every one was scored"
    matches = (await db_session.scalars(select(Match).where(Match.profile_id == profile.id))).all()
    assert len(matches) >= 1, "a cold start produces a feed, it does not withhold one"


async def test_below_the_threshold_the_unweighted_embedder_is_used(db_session) -> None:
    """Which is the fallback, and the row records that it was used."""
    await _profile(db_session)
    await _postings(db_session, 10)

    view = await refresh_corpus_stats(db_session)
    assert view.frequencies.usable is False
    assert _embedder_for(view) is None, "no weighted embedder below the threshold"

    await run_matching_pass(db_session)
    stamps = set((await db_session.scalars(select(Posting.embedding_model))).all())
    assert stamps == {"lexical@2"}, f"expected the unweighted stamp, got {stamps}"


async def test_at_the_threshold_the_weighted_embedder_takes_over(db_session) -> None:
    """And the stamp changes, so the two spaces are never compared silently."""
    await _profile(db_session)
    await _postings(db_session, MIN_DOCUMENTS + 5)

    view = await refresh_corpus_stats(db_session)
    assert view.frequencies.usable is True
    assert _embedder_for(view) is not None

    report = await run_matching_pass(db_session)

    assert report.revision == 1
    stamps = set((await db_session.scalars(select(Posting.embedding_model))).all())
    assert stamps == {"lexical-idf@2"}, f"expected the weighted stamp, got {stamps}"


async def test_a_posting_with_no_description_is_visible_but_not_embedded(db_session) -> None:
    """The one legitimate "matching pending" case, and it is not the threshold.

    `embed_postings` skips a posting with no `description_raw` — a Workday
    listing, per CLAUDE.md §15 — so it has no vector and cannot be scored on its
    body. It is still a stored, visible posting, which is why the status surface
    reports fetched and matched separately rather than implying one follows the
    other.
    """
    await _profile(db_session)
    db_session.add(
        Posting(url="https://example.com/bare", external_id="bare", title="Platform Engineer")
    )
    await db_session.flush()

    await run_matching_pass(db_session)

    posting = await db_session.scalar(select(Posting).where(Posting.external_id == "bare"))
    assert posting is not None, "it is stored and listable"
    assert posting.description_embedding is None, "and honestly unembedded"
