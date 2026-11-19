"""Graph answers read the saved skill links (`skill_links.py`) once they exist.

"Which jobs match skills from my projects?" used to rebuild a sample of the
owner's 100 best matches in memory, 5-7 s a question, and never saw a posting
the matching pass had not scored. With the links saved it reads every open,
unskipped posting in the search area, and no graph question re-extracts a
linked posting's requirements. Before `make skill-links` has run, both
questions answer exactly as before.
"""

from __future__ import annotations

from sqlalchemy import select

from packages.core.models import Posting
from packages.matching import graph_rag, skill_links
from packages.matching.graph_rag import retrieve_graph
from tests.test_graph_rag import posting as make_posting
from tests.test_graph_rag import seed


async def _link_everything(session) -> None:
    ids = list((await session.scalars(select(Posting.id))).all())
    await skill_links.link_postings(session, ids)
    await session.commit()


async def test_project_jobs_read_every_open_posting_in_the_area(worker_session) -> None:
    matched = await seed(worker_session)
    # Never scored by the matching pass, so no sample of matches could hold it.
    unscored = make_posting(company_id=matched.company_id, title="Platform Engineer")
    worker_session.add(unscored)
    await worker_session.commit()
    await _link_everything(worker_session)

    result = await retrieve_graph(worker_session, "projects")

    assert result.scope == "open_postings"
    # The closed, the London and the skipped postings from `seed` stay out.
    assert {p.posting_id for p in result.found.passages} == {matched.id, unscored.id}
    assert result.found.searched == 2
    assert "every open, unskipped posting" in result.summary
    assert "Based on all 2 open jobs" in result.answer_note


async def test_linked_postings_are_not_read_again(worker_session, monkeypatch) -> None:
    await seed(worker_session)
    await _link_everything(worker_session)

    def refuses(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("a linked posting's requirements were extracted again")

    monkeypatch.setattr(graph_rag, "fresh_requirements", refuses)

    projects = await retrieve_graph(worker_session, "projects")
    gaps = await retrieve_graph(worker_session, "gaps")

    assert projects.evidence
    assert gaps.scope == "open_postings"


async def test_without_saved_links_the_sample_of_matches_is_read_as_before(
    worker_session,
) -> None:
    matched = await seed(worker_session)

    result = await retrieve_graph(worker_session, "projects")

    assert result.scope == "top_matches"
    assert [p.posting_id for p in result.found.passages] == [matched.id]
    assert "bounded sample" in result.summary


async def test_postings_not_linked_yet_are_counted(worker_session) -> None:
    matched = await seed(worker_session)
    await skill_links.link_postings(worker_session, [matched.id])
    await worker_session.commit()

    result = await retrieve_graph(worker_session, "projects")

    # `seed`'s London and skipped postings are open and were not linked.
    assert result.scope == "open_postings"
    assert "2 open postings have no saved requirements yet" in result.summary
    assert "2 open jobs are not linked yet" in result.answer_note


async def test_a_posting_linked_from_older_text_is_read_from_its_text(worker_session) -> None:
    """Stale links are not trusted: the posting falls back to its own reading."""
    matched = await seed(worker_session)
    await _link_everything(worker_session)
    row = await worker_session.get(Posting, matched.id)
    assert row is not None
    row.content_hash = "edited since it was linked"
    await worker_session.commit()

    links = await graph_rag.saved_links(worker_session, [matched.id])

    assert matched.id not in links


async def test_gaps_read_every_open_posting_in_the_area(worker_session) -> None:
    matched = await seed(worker_session)
    # Never scored, so no sample of matches could hold it. Both ask for PostgreSQL,
    # which the seeded résumé does not list.
    unscored = make_posting(company_id=matched.company_id, title="Platform Engineer")
    worker_session.add(unscored)
    await worker_session.commit()
    await _link_everything(worker_session)

    result = await retrieve_graph(worker_session, "gaps")

    assert result.scope == "open_postings"
    # The closed, the London and the skipped postings from `seed` stay out.
    assert result.found.searched == 2
    assert "state at least one direct requirement" in result.summary
    assert any(
        "PostgreSQL" in item.text and "2 of 2 open jobs" in item.text for item in result.evidence
    )
    assert "Based on all 2 open jobs in your search area that state a direct requirement" in (
        result.answer_note
    )


async def test_an_on_site_posting_elsewhere_counts_when_its_text_says_remote(
    worker_session,
) -> None:
    matched = await seed(worker_session)
    body = "Requirements\nPython and PostgreSQL are required.\n"
    remote = make_posting(
        company_id=matched.company_id,
        location="Austin, TX",
        body=body + "This is a fully remote position.",
    )
    onsite = make_posting(company_id=matched.company_id, location="Dallas, TX", body=body)
    worker_session.add_all([remote, onsite])
    await worker_session.commit()
    await _link_everything(worker_session)

    result = await retrieve_graph(worker_session, "gaps")

    # The seeded in-area job and the remote Austin one; on-site Dallas stays out.
    assert result.found.searched == 2
