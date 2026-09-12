"""Score what changed, with weights from the corpus rather than the batch.

Two separable claims, and the second is the one that could go wrong quietly.

The set of postings needing work is derived in SQL, not handed over by the
crawler — so a posting missed because a worker died or a profile was added
between cycles turns up in the next pass rather than never.

And a subset is scored with corpus-wide term weights. Weighting forty new
postings by their rarity among those forty is docs/REFERENCE.md §3.6 in
different clothes: the numbers still come out, still get stored, and rank
against the rest of the feed as though they meant the same thing.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from packages.core.models import Candidate, Match, Posting, Profile, Project, User
from packages.matching.idf import (
    MIN_DOCUMENTS,
    DocumentFrequencies,
    open_document_count,
    refresh_corpus_stats,
)
from packages.matching.incremental import (
    postings_needing_embedding,
    postings_needing_scoring,
    run_matching_pass,
)


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
    # Something for the profile vector to be built from. An empty profile
    # encodes to zeros and scores everything 0.0, which passes assertions
    # about "a number came out" while testing nothing about the number.
    db_session.add(
        Project(
            candidate_id=candidate.id,
            source="github",
            external_id=f"{label}-1",
            name="telemetry-pipeline",
            full_name=f"owner/{label}-pipeline",
            url=f"https://github.com/owner/{label}",
            description="python django postgres service handling telemetry ingestion",
            language="Python",
            topics_json=["python", "django", "postgres"],
        )
    )
    await db_session.flush()
    return profile


async def _postings(db_session, count: int, *, prefix: str = "Backend Engineer") -> list[Posting]:
    made = [
        Posting(
            url=f"https://example.com/{prefix}/{n}",
            external_id=f"{prefix}-{n}",
            title=f"{prefix} {n}",
            description_raw=f"python django postgres team benefits role number {n}",
        )
        for n in range(count)
    ]
    db_session.add_all(made)
    await db_session.flush()
    return made


# --------------------------------------------------------------------------
# Deriving the work rather than being told it
# --------------------------------------------------------------------------


async def test_a_posting_with_no_vector_needs_embedding(db_session) -> None:
    await _postings(db_session, 3)

    pending = await postings_needing_embedding(db_session, revision=1, limit=100)

    assert len(pending) == 3


async def test_an_embedded_posting_is_left_alone(db_session) -> None:
    postings = await _postings(db_session, 2)
    await run_matching_pass(db_session)

    pending = await postings_needing_embedding(db_session, revision=None, limit=100)

    assert pending == [], f"{len(pending)} already-embedded postings would be redone"
    assert all(p.description_embedding is not None for p in postings)


async def test_a_closed_posting_is_not_work(db_session) -> None:
    """The feed is for things that can still be applied to."""
    from datetime import UTC, datetime

    postings = await _postings(db_session, 2)
    postings[0].closed_at = datetime.now(UTC)
    await db_session.flush()

    pending = await postings_needing_embedding(db_session, revision=1, limit=100)

    assert len(pending) == 1


async def test_a_posting_stamped_with_another_revision_is_redone(db_session) -> None:
    """Cosine across two corpus revisions does not fail; it returns a number."""
    await _postings(db_session, 2)
    await run_matching_pass(db_session)

    pending = await postings_needing_embedding(db_session, revision=999, limit=100)

    assert len(pending) == 2


async def test_a_profile_added_later_gets_the_whole_feed(db_session) -> None:
    """The case a crawler-supplied list of changed ids cannot cover.

    Nothing changed — the postings were there first. A list of what the crawl
    touched would be empty, and the new profile would have an empty feed that
    looks exactly like a feed with nothing in it.
    """
    await _postings(db_session, 3)
    first = await _profile(db_session, "first")
    await run_matching_pass(db_session)
    assert len(await postings_needing_scoring(db_session, first, limit=100)) == 0

    later = await _profile(db_session, "later")

    assert len(await postings_needing_scoring(db_session, later, limit=100)) == 3


async def test_a_posting_missed_by_one_pass_is_picked_up_by_the_next(db_session) -> None:
    """Derived, so nothing has to remember what was skipped."""
    profile = await _profile(db_session)
    await _postings(db_session, 5)

    await run_matching_pass(db_session, limit=2)
    remaining = await postings_needing_scoring(db_session, profile, limit=100)

    assert len(remaining) == 3, "the rest is still outstanding, not lost"
    await run_matching_pass(db_session, limit=100)
    assert await postings_needing_scoring(db_session, profile, limit=100) == []


async def test_a_pass_scores_and_stores(db_session) -> None:
    profile = await _profile(db_session)
    await _postings(db_session, 3)

    report = await run_matching_pass(db_session)

    assert report.scored == 3
    matches = (await db_session.scalars(select(Match).where(Match.profile_id == profile.id))).all()
    assert len(matches) >= 1, "a scored posting the profile can do should be stored"


async def test_a_second_pass_does_nothing(db_session) -> None:
    """The property that makes this runnable every few minutes."""
    await _profile(db_session)
    await _postings(db_session, 3)
    await run_matching_pass(db_session)

    second = await run_matching_pass(db_session)

    assert second.embedded == 0
    assert second.scored == 0


# --------------------------------------------------------------------------
# Corpus statistics
# --------------------------------------------------------------------------


async def test_the_corpus_is_counted_not_read(db_session) -> None:
    """Answering "has it grown 25%" needs a count, not the documents.

    `rebuild_if_stale` built the full term statistics to decide, then usually
    threw them away — reading every open posting's title and description on
    the path where the answer is "no", which is nearly every cycle.
    """
    await _postings(db_session, 4)

    assert await open_document_count(db_session) == 4


async def test_a_blank_posting_is_not_a_document(db_session) -> None:
    """The count has to apply the same condition `from_texts` does."""
    db_session.add(Posting(url="https://example.com/blank", external_id="blank"))
    await db_session.flush()

    assert await open_document_count(db_session) == 0
    assert DocumentFrequencies.from_texts(["", "  "]).total == 0


async def test_a_small_corpus_leaves_the_statistics_alone(db_session) -> None:
    """Below MIN_DOCUMENTS the statistic describes the sample, not the domain."""
    await _postings(db_session, 5)

    view = await refresh_corpus_stats(db_session)

    assert view.rebuilt is False
    assert view.revision is None


async def test_a_corpus_past_the_threshold_is_built_once(db_session) -> None:
    await _postings(db_session, MIN_DOCUMENTS + 5)

    first = await refresh_corpus_stats(db_session)
    second = await refresh_corpus_stats(db_session)

    assert first.rebuilt is True
    assert first.frequencies.usable
    assert second.rebuilt is False, "holding still between rebuilds is the point"
    assert second.revision == first.revision


async def test_a_rebuild_makes_the_next_pass_a_full_one(db_session) -> None:
    """A new revision means every stored vector was weighted by statistics
    that no longer exist."""
    await _profile(db_session)
    await _postings(db_session, 10)
    await run_matching_pass(db_session)

    # Push the corpus past the growth threshold.
    await _postings(db_session, MIN_DOCUMENTS + 10, prefix="Platform Engineer")
    report = await run_matching_pass(db_session)

    assert report.rebuilt is True
    assert report.embedded >= MIN_DOCUMENTS, "the whole corpus is re-embedded, not just the new"


async def test_a_subset_is_explained_with_corpus_weights_not_its_own(db_session) -> None:
    """The §3.6 trap: a measure computed against its own sample.

    This does not move the score — similarity comes from the embedder. It
    moves `missing_terms` and the legitimacy assessment, both written into
    `Match.reasons_json` and both read by the owner when deciding whether to
    apply. Among forty postings, a term in twenty of them reads as
    boilerplate and is dropped from the gap report; against the real corpus it
    is a skill the profile is missing.
    """
    from packages.matching.score import score_and_store

    profile = await _profile(db_session)
    await _postings(db_session, MIN_DOCUMENTS + 20)
    # A handful of postings asking for something rare in the corpus at large.
    rare = [
        Posting(
            url=f"https://example.com/rare/{n}",
            external_id=f"rare-{n}",
            title=f"Platform Engineer {n}",
            description_raw="kubernetes terraform kubernetes service mesh kubernetes",
        )
        for n in range(3)
    ]
    db_session.add_all(rare)
    await db_session.flush()

    view = await refresh_corpus_stats(db_session)
    assert view.frequencies.usable

    with_corpus = await score_and_store(
        db_session, profile, list(rare), frequencies=view.frequencies
    )
    from_batch = await score_and_store(db_session, profile, list(rare))

    corpus_gaps = {term for scored in with_corpus for term in scored.missing_terms}
    batch_gaps = {term for scored in from_batch for term in scored.missing_terms}

    assert corpus_gaps != batch_gaps, (
        "batch-derived weights produced the same gap report as corpus weights — "
        "this test can no longer tell them apart"
    )
    assert "kubernetes" in corpus_gaps, (
        "against the whole corpus kubernetes is rare, and the profile lacks it"
    )
    # The difference is not that the batch measures *badly*. A batch of three
    # is below `MIN_DOCUMENTS`, so its statistics are not `usable` at all and
    # the gap report silently falls back to the hand-written stopword list —
    # which is the thing IDF was introduced to replace. "engineer" is in every
    # posting in this corpus, so measurement calls it boilerplate and the
    # fallback does not.
    assert "engineer" not in corpus_gaps, "measured against the corpus, this is boilerplate"
    assert "engineer" in batch_gaps, (
        "without corpus statistics the report degrades to the hand-written list"
    )


@pytest.mark.parametrize("full", [False, True])
async def test_a_full_pass_takes_everything(db_session, full) -> None:
    profile = await _profile(db_session)
    await _postings(db_session, 3)
    await run_matching_pass(db_session)

    outstanding = await postings_needing_scoring(db_session, profile, limit=100, full=full)

    assert len(outstanding) == (3 if full else 0)
