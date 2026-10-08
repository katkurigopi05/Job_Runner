"""Grading postings by conversation, over MCP.

The labeling loop needs a hundred or more of the owner's grades and has none
(`docs/BACKLOG.md` P1). Grading in a conversation is one more way to give
them, and it has one way to go wrong that the dashboard does not: the one
calling the tool is a model. A grade a model chose is the ranker being marked
by another model, stamped `owner`, which is the provenance a benchmark trusts
most.

So the grade is typed by the owner into a form, as an application's missing
answers are, and three other things are kept from the model: the ranker's
score, the stream a posting was drawn from, and the choice of which posting a
grade lands on.
"""

from __future__ import annotations

import inspect
import json
import uuid
from typing import Any

import pytest
from mcp.client import Client
from sqlalchemy import select

from apps.mcp import server as mcp_server
from packages.core.models import PostingLabel
from packages.matching import active
from packages.matching.labels import RELEVANCE_SCALE
from tests import test_labeling_loop

# The binding to the ASGI app is the other file's, so the two cannot differ.
from tests.test_mcp import BOTH_PROTOCOLS, _bind_tools_to_app, _Owner, _wire, call  # noqa: F401

#: The labeling loop's own corpus: ten scored postings and five the ranker
#: never scored. Taken by name so the two files are grading the same thing.
corpus = test_labeling_loop.corpus


@pytest.fixture(autouse=True)
def _nothing_offered_yet(monkeypatch):
    """What the server remembers offering is per process. Each test starts clean."""
    monkeypatch.setattr(mcp_server, "_OFFERED", {})


def _picks(grade: int, note: str | None = None) -> Any:
    """An owner who chooses that grade from the form's own choices."""

    def typed(schema: dict[str, Any]) -> dict[str, Any]:
        [choice] = [c for c in schema["properties"]["grade"]["enum"] if c.startswith(f"{grade}:")]
        return {"grade": choice} if note is None else {"grade": choice, "note": note}

    return typed


async def _stored(worker_session, posting_id: str) -> PostingLabel | None:
    return await worker_session.scalar(
        select(PostingLabel).where(PostingLabel.posting_id == uuid.UUID(posting_id))
    )


# --------------------------------------------------------------------------
# What the model is given
# --------------------------------------------------------------------------


async def test_a_model_has_nowhere_to_put_a_grade() -> None:
    """Read from the wire entry: what a client is told it may pass."""
    tools = {t.name: _wire(t) for t in await mcp_server.server.list_tools()}

    # Which offer is being graded, and nothing about how.
    assert set(tools["grade_posting"]["inputSchema"]["properties"]) == {"posting_id", "profile_id"}


async def test_a_posting_comes_without_the_rankers_opinion_of_it(corpus) -> None:
    """`/label` shows the owner the score and the stream, as two values beside
    the posting. Here the model writes everything the owner reads about it,
    and one that knows the ranker's opinion can lean its description that way
    without quoting it."""
    offered = await call("next_to_grade", size=6)

    assert offered["count"] == 6
    for posting in offered["postings"]:
        # The exact set, so adding a field is a deliberate act.
        assert set(posting) == {
            "posting_id",
            "title",
            "location",
            "url",
            "description",
            "first_seen_at",
        }
    sent = json.dumps(offered)
    assert "score" not in sent and "stream" not in sent
    for stream in active.Stream:
        assert stream.value not in sent


async def test_nothing_to_grade_says_what_that_can_mean(complete_candidate) -> None:
    offered = await call("next_to_grade")

    assert offered["postings"] == [] and offered["count"] == 0
    assert "crawl" in offered["note"]


# --------------------------------------------------------------------------
# The owner grades
# --------------------------------------------------------------------------


@BOTH_PROTOCOLS
async def test_the_owner_picks_the_grade_from_the_scale(corpus, worker_session, mode) -> None:
    [posting] = (await call("next_to_grade", size=1))["postings"]
    owner = _Owner(_picks(2), mode=mode)

    graded = await owner.calls("grade_posting", posting_id=posting["posting_id"])

    assert graded == {
        "graded": True,
        "posting_id": posting["posting_id"],
        "relevance": 2,
        "note": None,
    }
    stored = await _stored(worker_session, posting["posting_id"])
    assert stored is not None and stored.relevance == 2

    [form] = owner.shown
    # The scale the ranking metrics use, in its own words, and nothing else.
    assert form["properties"]["grade"]["enum"] == [
        f"{grade}: {meaning}" for grade, meaning in sorted(RELEVANCE_SCALE.items())
    ]
    assert form["required"] == ["grade"]
    # Which posting the form is about is said by the server, from what it
    # offered. What is on screen in the conversation is the model's to write.
    [message] = owner.messages
    assert posting["title"] in message


async def test_the_owners_note_goes_with_the_grade(corpus, worker_session) -> None:
    [posting] = (await call("next_to_grade", size=1))["postings"]

    graded = await _Owner(_picks(0, "wrong level")).calls(
        "grade_posting", posting_id=posting["posting_id"]
    )

    assert graded["note"] == "wrong level"
    assert (await _stored(worker_session, posting["posting_id"])).note == "wrong level"


async def test_a_grade_can_be_given_again(corpus, worker_session) -> None:
    """A corpus that cannot be corrected is one built slowly."""
    [posting] = (await call("next_to_grade", size=1))["postings"]

    await _Owner(_picks(3)).calls("grade_posting", posting_id=posting["posting_id"])
    await _Owner(_picks(1)).calls("grade_posting", posting_id=posting["posting_id"])

    assert (await _stored(worker_session, posting["posting_id"])).relevance == 1
    assert (await call("grading_progress"))["total"] == 1


@pytest.mark.parametrize("action", ["decline", "cancel"])
async def test_an_owner_who_closes_the_form_has_graded_nothing(corpus, action) -> None:
    [posting] = (await call("next_to_grade", size=1))["postings"]

    graded = await _Owner(action=action).calls("grade_posting", posting_id=posting["posting_id"])

    assert graded["graded"] is False and "closed" in graded["error"]
    assert (await call("grading_progress"))["total"] == 0


async def test_a_client_that_cannot_show_a_form_is_sent_to_the_label_page(corpus) -> None:
    """The posting is not handed to the model to grade in the owner's place."""
    [posting] = (await call("next_to_grade", size=1))["postings"]

    async with Client(mcp_server.server) as client:
        result = await client.call_tool("grade_posting", {"posting_id": posting["posting_id"]})

    assert result.is_error
    assert "/label" in result.content[0].text and "/review" not in result.content[0].text
    assert (await call("grading_progress"))["total"] == 0


# --------------------------------------------------------------------------
# Which posting, and where it came from
# --------------------------------------------------------------------------


async def test_a_posting_this_server_did_not_offer_is_not_graded(corpus) -> None:
    """The form names the posting from what the server offered. An id it never
    offered has no name to show, so the owner would be grading whatever the
    conversation said it was."""
    owner = _Owner(_picks(3))

    result = await owner.result("grade_posting", posting_id=corpus["scored"][0])

    assert result.is_error
    assert "next_to_grade" in result.content[0].text
    assert owner.shown == [], "the owner was asked about a posting nobody offered"
    assert (await call("grading_progress"))["total"] == 0


async def test_the_stream_a_posting_was_drawn_from_goes_with_its_grade(
    corpus, worker_session
) -> None:
    """`unseen` is recorded only when a serve attests it (`routers/labels.py`),
    and a posting the ranker never scored is the one label only this loop can
    produce. The tool does not show the stream and does not take it back as an
    argument: it remembers what it was served."""
    offered = (await call("next_to_grade", size=10))["postings"]
    never_scored = [p for p in offered if p["posting_id"] in corpus["unscored"]]
    assert never_scored, "the batch held no posting from the unseen stream"

    await _Owner(_picks(1)).calls("grade_posting", posting_id=never_scored[0]["posting_id"])

    stored = await _stored(worker_session, never_scored[0]["posting_id"])
    assert stored.stream == active.Stream.UNSEEN.value
    assert (await call("grading_progress"))["by_stream"] == {"unseen": 1}


async def test_a_grade_goes_to_the_profile_the_posting_was_offered_for(monkeypatch) -> None:
    api = _Recording([{**OFFER, "posting_id": "post-1"}])
    monkeypatch.setattr(mcp_server, "_client", api)

    await call("next_to_grade", profile_id="p-2")
    await _Owner(_picks(2)).calls("grade_posting", posting_id="post-1", profile_id="p-2")

    [(_, _, _, body)] = [asked for asked in api.asked if asked[0] == "POST"]
    assert body["profile_id"] == "p-2"


async def test_an_offer_to_one_profile_is_not_an_offer_to_another(monkeypatch) -> None:
    """An offer is a posting served to one profile. The same posting named for
    a profile it was not offered to is one this server never offered, and is
    refused before the owner is asked anything."""
    api = _Recording([OFFER])
    monkeypatch.setattr(mcp_server, "_client", api)
    await call("next_to_grade", profile_id="p-2")

    for other in ({}, {"profile_id": "p-1"}):
        owner = _Owner(_picks(3))
        result = await owner.result("grade_posting", posting_id="post-1", **other)

        assert result.is_error and "profile_id" in result.content[0].text
        assert owner.shown == []
    assert [asked for asked in api.asked if asked[0] == "POST"] == []


@BOTH_PROTOCOLS
async def test_a_request_made_while_the_form_is_open_does_not_move_the_grade(
    monkeypatch, mode
) -> None:
    """Two profiles can be served one posting. What the server remembered was
    kept by posting alone, so the second offer replaced the first: a form
    opened for one profile was recorded under the other, with the other's
    stream. Both protocols, because the newer one reads the offer again after
    the form is answered."""
    api = _ServedByProfile([OFFER])
    monkeypatch.setattr(mcp_server, "_client", api)
    await call("next_to_grade", profile_id="p-1")

    await _Interrupted(_picks(2), mode=mode).calls(
        "grade_posting", posting_id="post-1", profile_id="p-1"
    )

    [body] = [body for method, _, _, body in api.asked if method == "POST"]
    assert (body["profile_id"], body["served_stream"]) == ("p-1", "unseen")


# --------------------------------------------------------------------------
# Progress
# --------------------------------------------------------------------------


async def test_progress_says_how_far_and_what_the_corpus_still_lacks(corpus) -> None:
    [posting] = (await call("next_to_grade", size=1))["postings"]
    await _Owner(_picks(2)).calls("grade_posting", posting_id=posting["posting_id"])

    progress = await call("grading_progress")

    assert progress["total"] == 1 and progress["usable"] is False
    assert progress["remaining"] == progress["target"] - 1
    assert progress["notes"]


# --------------------------------------------------------------------------
# What is sent
# --------------------------------------------------------------------------

OFFER = {
    "posting_id": "post-1",
    "title": "Staff Platform Engineer",
    "location": "San Francisco, CA",
    "url": "https://example.test/jobs/1",
    "description": "Python and Postgres.",
    "first_seen_at": "2026-10-06T10:00:00Z",
    "stream": "unseen",
    "score": None,
}


class _Recording:
    """Stands in for the API: what was asked of it, and the postings it offers."""

    def __init__(self, offers: list[dict[str, Any]]) -> None:
        self.offers = offers
        self.asked: list[tuple[str, str, dict[str, Any], Any]] = []

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.asked.append((method, path, kwargs.get("params") or {}, kwargs.get("json")))
        if path == "/labels/next":
            return self.offers
        if method == "POST":
            return {"id": "l-1", **kwargs["json"]}
        return {"total": 0}


class _ServedByProfile(_Recording):
    """The same posting for either profile, each from a stream of its own."""

    STREAMS = {"p-1": "unseen", "p-2": "confident"}

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        if path != "/labels/next":
            return await super().request(method, path, **kwargs)
        self.asked.append((method, path, kwargs["params"], None))
        return [{**OFFER, "stream": self.STREAMS[kwargs["params"]["profile_id"]]}]


class _Interrupted(_Owner):
    """An owner with the form open while another request reaches the server."""

    async def _answer(self, context: Any, params: Any) -> Any:
        await call("next_to_grade", profile_id="p-2")
        return await super()._answer(context, params)


async def test_grading_writes_one_label_and_touches_nothing_else(monkeypatch) -> None:
    """A grade is a statement about a posting. It applies to nothing, decides
    no match and queues no work."""
    api = _Recording([OFFER])
    monkeypatch.setattr(mcp_server, "_client", api)

    await call("next_to_grade")
    await _Owner(_picks(3, "exactly this")).calls("grade_posting", posting_id="post-1")
    await call("grading_progress")

    assert [(method, path) for method, path, _, _ in api.asked] == [
        ("GET", "/labels/next"),
        ("POST", "/labels"),
        ("GET", "/labels/summary"),
    ]
    [body] = [body for method, _, _, body in api.asked if method == "POST"]
    assert body == {
        "posting_id": "post-1",
        "profile_id": None,
        "relevance": 3,
        "note": "exactly this",
        "served_stream": "unseen",
    }


async def test_every_parameter_sent_is_one_its_route_reads(monkeypatch) -> None:
    from apps.api.routers import labels

    api = _Recording([OFFER])
    monkeypatch.setattr(mcp_server, "_client", api)

    await call("next_to_grade", size=5, profile_id="p-1")
    await call("grading_progress", profile_id="p-1")

    reads = {
        "/labels/next": set(inspect.signature(labels.next_to_label).parameters),
        "/labels/summary": set(inspect.signature(labels.summary).parameters),
    }
    for _, path, params, _ in api.asked:
        assert params and params.keys() <= reads[path], (path, params.keys() - reads[path])
