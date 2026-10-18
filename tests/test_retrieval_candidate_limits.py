"""A posting the search area or a level keeps is never cut off by ones they drop.

Phase 5 of the 2026-10-15 harness audit (`docs/HARNESS_AUDIT_2026-10-15.md`,
section F): "Retrieval area/level filtering follows the initial limited
keyword/vector candidate scan in some branches." Each search below reads a
fixed number of candidates, best first, and only then asks the area or the
level about them. When more candidates than that are left out and all rank
above one that is kept, the kept one is never read, and the answer says there
is none: "none in the search area", "0 state they sponsor".

Each test is aimed at one of those pools and puts more dropped candidates
above one kept posting than the pool holds. The `test_retrieval_area.py` test
of the same shape uses 35, which the 120-posting area pool absorbs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from packages.core.config import Settings, get_settings
from packages.core.models import Company, CorpusStats, Posting
from packages.core.models_chunks import PostingChunk
from packages.matching import retrieve as R
from packages.matching.embed import EMBEDDING_DIM, SentenceTransformerEmbedder
from tests.test_chat_cross_encoder import WordJudge

NOW = datetime.now(UTC)
#: The kept posting is older than every dropped one, so it ranks last on a tie.
OLDER = NOW - timedelta(days=30)
ABROAD = "UK - London"
HERE = "San Francisco, CA"

#: A chunk at the question, and one a little way from it.
NEAREST = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
FARTHER = [0.8, 0.6] + [0.0] * (EMBEDDING_DIM - 2)


@pytest.fixture(autouse=True)
def standing_area(monkeypatch):
    """The shipped preference, whatever the machine running this has set."""
    monkeypatch.setenv("SEARCH_US_ONLY", "true")
    monkeypatch.setenv("SEARCH_REMOTE_OUTSIDE_CALIFORNIA", "true")
    monkeypatch.setenv("CHAT_ALLOW_UNKNOWN_SENIORITY", "false")
    get_settings.cache_clear()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


class AtTheQuestion:
    """Encodes every question to `NEAREST`. No model is loaded."""

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [list(NEAREST) for _ in texts]


@pytest.fixture
def chunk_space(monkeypatch):
    """Questions encode into the bge-small chunk space, and nothing else."""
    monkeypatch.setattr(
        R,
        "_encoders",
        lambda spaces, *_: {
            space: AtTheQuestion()
            for space in spaces
            if space[0] == SentenceTransformerEmbedder.name
        },
    )


async def _company(session, name: str = "Acme") -> Company:
    company = Company(name=name, ats_type="greenhouse")
    session.add(company)
    await session.flush()
    return company


async def _postings(
    session,
    company: Company,
    count: int,
    *,
    location: str,
    title: str,
    body: str,
    first_seen: datetime = NOW,
    chunks: int = 0,
    vector: list[float] = NEAREST,
) -> list[Posting]:
    """`count` postings, newest first. `{n}` in the title is the posting's number.

    With `chunks`, each gets that many chunk vectors at `vector`.
    """
    made = [
        Posting(
            company_id=company.id,
            external_id=uuid.uuid4().hex[:12],
            url=f"https://x.test/{uuid.uuid4().hex[:12]}",
            title=title.format(n=n),
            location=location,
            description_raw=body,
            content_hash="h1",
            first_seen_at=first_seen - timedelta(seconds=n),
        )
        for n in range(count)
    ]
    session.add_all(made)
    await session.flush()
    if chunks:
        session.add_all(
            PostingChunk(
                posting_id=posting.id,
                ordinal=ordinal,
                start=0,
                length=len(body),
                content_hash="h1",
                embedding_model=SentenceTransformerEmbedder.name,
                embedding=list(vector),
            )
            for posting in made
            for ordinal in range(chunks)
        )
        await session.flush()
    return made


def _shown(found) -> list[uuid.UUID]:
    return [p.posting_id for p in found.passages] + [m.posting_id for m in found.more]


# --- the keyword scan ---------------------------------------------------------------


async def test_an_in_area_match_below_the_whole_area_pool_is_found(db_session) -> None:
    """Every foreign posting names PyTorch in its title too, which counts
    twice, so all of them outrank the one in San Francisco."""
    company = await _company(db_session)
    abroad = R._AREA_POOL + 5
    await _postings(
        db_session, company, abroad, location=ABROAD, title="PyTorch Engineer {n}", body="PyTorch."
    )
    (here,) = await _postings(
        db_session, company, 1, location=HERE, title="Engineer", body="PyTorch.", first_seen=OLDER
    )

    found = await R.retrieve(db_session, "pytorch jobs")

    assert _shown(found) == [here.id]
    assert found.outside_area == abroad, "every foreign match that was read is counted"


async def test_a_match_at_the_level_below_the_whole_level_pool_is_found(db_session) -> None:
    """Asked for junior, and every newer posting saying Kafka is senior."""
    company = await _company(db_session)
    senior = R._LEVEL_POOL + 5
    await _postings(
        db_session, company, senior, location=HERE, title="Senior Kafka Engineer {n}", body="Kafka."
    )
    (junior,) = await _postings(
        db_session,
        company,
        1,
        location=HERE,
        title="Junior Kafka Engineer",
        body="Kafka.",
        first_seen=OLDER,
    )

    found = await R.retrieve(db_session, "junior kafka jobs")

    assert _shown(found) == [junior.id]
    assert found.level is not None
    assert (found.level.at_level, found.level.other) == (1, senior)


async def test_a_whole_title_in_area_goes_first_below_a_pool_of_foreign_ones(db_session) -> None:
    """Asked for by its whole title, a posting goes first (§14). The newest 30
    postings with that title are abroad, and the one here lost its place to a
    newer posting that only shares the words."""
    company = await _company(db_session)
    title = "Ledger Operations Analyst"
    await _postings(
        db_session,
        company,
        R.KEYWORD_POOL + 5,
        location=ABROAD,
        title=title,
        body="Ledger operations.",
    )
    (named,) = await _postings(
        db_session,
        company,
        1,
        location=HERE,
        title=title,
        body="Ledger operations.",
        first_seen=OLDER,
    )
    (sharing,) = await _postings(
        db_session,
        company,
        1,
        location=HERE,
        title="Ledger Operations Analyst, Payments",
        body="Ledger operations.",
    )

    found = await R.retrieve(db_session, "ledger operations analyst jobs")

    assert _shown(found)[:2] == [named.id, sharing.id]


async def test_an_offer_below_the_whole_sponsorship_pool_is_found_and_counted(
    db_session,
) -> None:
    """Offers are rare (15 of 5,204 in the owner's area), and each refusal
    newer than this one took a place in the pool of 500 it was cut from."""
    company = await _company(db_session)
    refusing = 505
    await _postings(
        db_session,
        company,
        refusing,
        location=HERE,
        title="Kafka Engineer {n}",
        body="Kafka. We are unable to sponsor visas for this position.",
    )
    (offers,) = await _postings(
        db_session,
        company,
        1,
        location=HERE,
        title="Kafka Engineer",
        body="Kafka. We offer visa sponsorship for this role.",
        first_seen=OLDER,
    )

    found = await R.retrieve(db_session, "which kafka jobs offer visa sponsorship?")

    assert _shown(found) == [offers.id]
    assert found.sponsorship is not None
    assert (found.sponsorship.offered, found.sponsorship.refused) == (1, refusing)


# --- the vector scans ---------------------------------------------------------------


async def test_vectors_alone_reach_an_in_area_posting_below_the_chunk_pool(
    db_session, chunk_space
) -> None:
    """No word of the question is rare enough to search for, and none is in a
    title, so vectors answer (§14). Five chunks each from 45 foreign postings
    are nearer than the one posting here: past both the 200-chunk scan and
    the 40 postings it is cut to."""
    db_session.add(
        CorpusStats(revision=1, total_documents=100, counts_json={"data": 90, "engineering": 90})
    )
    company = await _company(db_session)
    body = "Build streaming pipelines for the platform team."
    await _postings(
        db_session,
        company,
        45,
        location=ABROAD,
        title="Pipeline Specialist {n}",
        body=body,
        chunks=5,
    )
    (here,) = await _postings(
        db_session,
        company,
        1,
        location=HERE,
        title="Pipeline Specialist",
        body=body,
        first_seen=OLDER,
        chunks=1,
        vector=FARTHER,
    )

    found = await R.retrieve(db_session, "any data engineering roles?")

    assert _shown(found) == [here.id]
    assert found.outside_area >= 45


async def test_semantic_candidates_reach_an_in_area_posting_below_the_chunk_pool(
    db_session, chunk_space, monkeypatch
) -> None:
    """The experiment (`CHAT_SEMANTIC_CANDIDATES`) read the 200 nearest
    chunks of the whole corpus and only then the area."""
    settings = Settings(_env_file=None, chat_semantic_candidates=True)
    monkeypatch.setattr(R, "get_settings", lambda: settings)
    company = await _company(db_session)
    body = "Build retrieval systems."
    await _postings(
        db_session,
        company,
        45,
        location=ABROAD,
        title="Search Engineer {n}",
        body=body,
        chunks=5,
    )
    (here,) = await _postings(
        db_session,
        company,
        1,
        location=HERE,
        title="Search Engineer",
        body=body,
        first_seen=OLDER,
        chunks=1,
        vector=FARTHER,
    )

    found = await R.retrieve(db_session, "jobs grounding answers")

    assert _shown(found) == [here.id]


async def test_a_judge_is_handed_an_in_area_posting_below_the_unfound_pool(
    db_session, chunk_space, monkeypatch
) -> None:
    """A model that judges is handed the nearest postings the keywords did
    not find (§21). It read the first 200 of them and only then the area, so
    200 foreign ones nearer the question left it nothing to judge."""
    judge = WordJudge("redpanda")
    monkeypatch.setattr(R, "get_reranker", lambda: judge)
    db_session.add(CorpusStats(revision=1, total_documents=100, counts_json={"streaming": 90}))
    company = await _company(db_session)
    (said,) = await _postings(
        db_session, company, 1, location=HERE, title="Data Engineer", body="Kafka streaming."
    )
    body = "Event streaming on Redpanda."
    await _postings(
        db_session,
        company,
        R._widen_pool() * 4 + 5,
        location=ABROAD,
        title="Platform Engineer {n}",
        body=body,
        chunks=1,
    )
    (here,) = await _postings(
        db_session,
        company,
        1,
        location=HERE,
        title="Platform Engineer",
        body=body,
        first_seen=OLDER,
        chunks=1,
        vector=FARTHER,
    )

    found = await R.retrieve(db_session, "kafka streaming roles")

    assert set(_shown(found)) == {said.id, here.id}
