"""What the assistant remembers of a conversation.

`/chat` took one message and nothing else, so "which of those are remote?"
had nothing to refer to: each question stood alone. The dashboard now sends
the last few exchanges with the question.

What is sent is the client's copy of earlier answers, and three of this
assistant's rules were written for a single message. Each has a way round it
once there are two:

- §14 keeps recruiter mail from a remote model unless the owner says so. An
  earlier answer written by the local model can quote that mail.
- §2.2 refuses a first-person question about salary, sponsorship, work
  authorization or employment history. Asked in two halves, neither message
  is that question.
- §14 grounds every answer in what the database holds. An earlier answer is
  the model's own prose.

So the server decides what of the history the model sees, and re-reads the
postings an earlier answer cited instead of trusting what it said of them.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient

from apps.api import chat_history
from packages.core.models import Company, Posting
from packages.llm.provider import StubProvider
from tests.test_chat_api import _application_with_mail

EARLIER = "EARLIER IN THIS CONVERSATION"
KAFKA = {"question": "Which open roles use Kafka?", "answer": "Two of them do."}


def _answering(text: str = "ok", model: str | None = None) -> tuple[type[StubProvider], dict]:
    """A provider that keeps the prompt it was given and answers with `text`."""
    seen: dict[str, str] = {}

    class Recorder(StubProvider):
        def __init__(self) -> None:
            super().__init__()
            if model is not None:
                self.model = model

        async def complete(
            self, system: str, user: str, *, max_tokens: int = 1024, temperature: float = 0.2
        ) -> str:
            seen["user"] = user
            return text

    return Recorder, seen


@pytest.fixture
def ask(client: AsyncClient, monkeypatch):
    """Ask a question with a history, and get back the reply and the prompt."""
    import apps.api.routers.chat as chat_module

    async def asking(message: str, history: list[dict[str, Any]], **extra: Any):
        answer = extra.pop("answers", "ok")
        model = "gemini-3.6-flash" if extra.get("provider") else None
        recorder, seen = _answering(answer, model)
        monkeypatch.setattr(chat_module.llm_router, "build_provider", lambda name=None: recorder())
        response = await client.post(
            "/chat", json={"message": message, "history": history, **extra}
        )
        assert response.status_code == 200, response.text
        return response.json(), seen.get("user", "")

    return asking


def _history_part(prompt: str) -> str:
    """The prompt between the context and the question."""
    return prompt.partition(EARLIER)[2].partition("\n\nQUESTION:\n")[0]


async def _posting(worker_session, title: str, location: str = "Remote, US") -> Posting:
    company = Company(name=f"Acme {uuid.uuid4().hex[:4]}", ats_type="greenhouse")
    worker_session.add(company)
    await worker_session.flush()
    posting = Posting(
        company_id=company.id,
        url=f"https://boards.greenhouse.io/acme/jobs/{uuid.uuid4().hex[:8]}",
        title=title,
        location=location,
        description_raw="Streaming pipelines.",
    )
    worker_session.add(posting)
    await worker_session.commit()
    posting.company_name = company.name  # for the assertions below
    return posting


# --------------------------------------------------------------------------
# Remembering
# --------------------------------------------------------------------------


async def test_an_earlier_exchange_reaches_the_model(ask) -> None:
    reply, prompt = await ask("which of those are remote?", [KAFKA])

    assert EARLIER in prompt
    assert prompt.index(EARLIER) < prompt.index("\n\nQUESTION:\n")
    assert KAFKA["question"] in _history_part(prompt)
    assert KAFKA["answer"] in _history_part(prompt)
    assert reply["history_used"] == 1 and reply["history_withheld"] == 0


async def test_a_first_question_is_asked_as_it_always_was(ask) -> None:
    reply, prompt = await ask("how many are waiting on me?", [])

    assert EARLIER not in prompt
    assert reply["history_used"] == 0


async def test_only_the_last_few_exchanges_are_kept_and_a_long_one_is_cut(ask) -> None:
    """The local model reads 4,096 tokens, and a prompt that does not fit is
    refused (§14). What gives way is the oldest exchange and the end of a long
    answer, not the question."""
    history = [
        {"question": f"question number {n}", "answer": f"answer {n} " + "word " * 400}
        for n in range(8)
    ]

    reply, prompt = await ask("and the next one?", history)

    kept = _history_part(prompt)
    assert reply["history_used"] == chat_history.MAX_TURNS
    for n in range(8 - chat_history.MAX_TURNS):
        assert f"question number {n}" not in kept
    assert "question number 7" in kept
    budget = chat_history.MAX_TURNS * (chat_history.QUESTION_CHARS + chat_history.ANSWER_CHARS)
    assert len(kept) < budget + 400


async def test_a_citation_in_an_earlier_answer_is_not_carried_over(ask) -> None:
    """`[P1]` meant a posting found for that question. In this prompt it names
    another one, and a model that repeats it marks the wrong posting as cited."""
    told = "Kafka is used at Acme [P1] and at Globex 【P2】, and twice more (P3, P4)."

    _, prompt = await ask("which of those are remote?", [{**KAFKA, "answer": told}])

    kept = _history_part(prompt).partition("POSTINGS FROM EARLIER")[0]
    assert "Kafka is used at Acme" in kept
    for label in ("P1", "P2", "P3", "P4"):
        assert label not in kept


async def test_a_request_with_far_too_much_history_is_refused(client: AsyncClient) -> None:
    response = await client.post("/chat", json={"message": "and then?", "history": [KAFKA] * 60})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


# --------------------------------------------------------------------------
# The postings an earlier answer cited
# --------------------------------------------------------------------------


async def test_a_posting_an_earlier_answer_cited_is_read_again(ask, worker_session) -> None:
    """Grounded, not freehand (§14). "Those" are the postings the earlier
    answer cited, and what the model is told of them comes from the database
    now, not from what it wrote then."""
    posting = await _posting(worker_session, "Data Engineer", "Remote, US")

    _, prompt = await ask(
        "what needs me?", [{**KAFKA, "answer": "Acme does.", "cited": [str(posting.id)]}]
    )

    section = prompt.partition("POSTINGS FROM EARLIER ANSWERS")[2]
    assert f"[P1] Data Engineer — {posting.company_name} — Remote, US" in section


async def test_an_earlier_posting_the_answer_uses_is_listed_under_it(ask, worker_session) -> None:
    posting = await _posting(worker_session, "Data Engineer")

    reply, _ = await ask(
        "what needs me?",
        [{**KAFKA, "cited": [str(posting.id)]}],
        answers="The Data Engineer role is remote [P1].",
    )

    assert [(s["posting_id"], s["label"], s["cited"]) for s in reply["sources"]] == [
        (str(posting.id), "P1", True)
    ]


async def test_an_earlier_posting_the_answer_does_not_use_is_not_listed(
    ask, worker_session
) -> None:
    """What sits under an answer is what the search found for it. A posting
    carried over from an earlier answer is listed only when this one cites it."""
    posting = await _posting(worker_session, "Data Engineer")

    reply, _ = await ask("what needs me?", [{**KAFKA, "cited": [str(posting.id)]}])

    assert reply["sources"] == []


async def test_a_posting_found_again_for_this_question_is_not_given_twice(
    ask, worker_session
) -> None:
    posting = await _posting(worker_session, "Kafka Platform Engineer")

    reply, prompt = await ask(
        "Which open roles use Kafka?", [{**KAFKA, "cited": [str(posting.id)]}]
    )

    assert [s["posting_id"] for s in reply["sources"]] == [str(posting.id)]
    assert prompt.count("Kafka Platform Engineer") == 1
    assert "POSTINGS FROM EARLIER ANSWERS" not in prompt


async def test_an_id_that_is_no_posting_is_passed_over(ask) -> None:
    reply, prompt = await ask("what needs me?", [{**KAFKA, "cited": [str(uuid.uuid4())]}])

    assert "POSTINGS FROM EARLIER ANSWERS" not in prompt
    assert reply["history_used"] == 1


# --------------------------------------------------------------------------
# §2.2, asked in two halves
# --------------------------------------------------------------------------

LISTED_PAY = {
    "question": "What salary does this posting list?",
    "answer": "It lists 150 to 180 thousand.",
}


async def test_a_refused_question_is_never_remembered(ask) -> None:
    """The dashboard does not send one. If something else did, the model still
    must not be handed a question the rule refused, with an answer beside it."""
    refused = {"question": "what should I put for my salary expectation?", "answer": "Say 200."}

    reply, prompt = await ask("which roles use Kafka?", [refused])

    assert "Say 200" not in prompt and "salary expectation" not in prompt
    assert reply["history_used"] == 0 and reply["history_withheld"] == 1


async def test_a_first_person_follow_up_is_not_given_an_exchange_about_pay(ask) -> None:
    """ "What salary does this posting list?" is a question about a posting and
    is answered. "What should I ask for?" names no protected topic and is not
    refused. Together they are the question §2.2 refuses, so the second is
    asked without the first."""
    reply, prompt = await ask("what should I ask for?", [LISTED_PAY])

    assert "150 to 180" not in prompt and "salary does this posting" not in prompt
    assert "left out" in prompt
    assert reply["provider"] != "refused"
    assert reply["history_used"] == 0 and reply["history_withheld"] == 1


async def test_a_follow_up_about_the_postings_is_given_it(ask) -> None:
    """The cost of the rule above has to stay small. "Which roles offer
    sponsorship?" and then "which of those are in California?" is the
    conversation this owner most needs to work."""
    sponsoring = {"question": "Which roles offer visa sponsorship?", "answer": "Three say so."}

    reply, prompt = await ask("which of those are in California?", [sponsoring])

    assert "Three say so." in _history_part(prompt)
    assert reply["history_used"] == 1


async def test_the_question_itself_is_still_refused_whatever_came_before(ask) -> None:
    reply, prompt = await ask("what should I put for my salary?", [KAFKA])

    assert reply["provider"] == "refused"
    assert prompt == "", "a refused question reached a model"


# --------------------------------------------------------------------------
# Recruiter mail, in an earlier answer
# --------------------------------------------------------------------------

ABOUT_MAIL = {
    "question": "any replies?",
    "answer": "The recruiter at bigco wrote about scheduling a call.",
    "mail_in_context": True,
}


async def test_an_answer_written_with_mail_in_hand_does_not_go_to_a_remote_model(ask) -> None:
    """The local model always sees the mail (§14), so its answer can quote it.
    Sent on as history, that is the mail reaching a remote model with the box
    unticked."""
    reply, prompt = await ask("and then what?", [ABOUT_MAIL], provider="gemini")

    assert "bigco" not in prompt
    # Said, as the withheld mail itself is: an absent exchange would read as
    # nothing having been said.
    assert "withheld" in prompt
    assert reply["history_used"] == 0 and reply["history_withheld"] == 1


async def test_it_goes_when_the_owner_shares_mail_for_the_question(ask) -> None:
    _, prompt = await ask("and then what?", [ABOUT_MAIL], provider="gemini", share_mail=True)

    assert "bigco" in _history_part(prompt)


async def test_it_goes_to_the_local_model(ask) -> None:
    _, prompt = await ask("and then what?", [ABOUT_MAIL])

    assert "bigco" in _history_part(prompt)


async def test_an_exchange_that_does_not_say_is_treated_as_having_had_mail(ask) -> None:
    unmarked = {key: value for key, value in ABOUT_MAIL.items() if key != "mail_in_context"}

    _, prompt = await ask("and then what?", [unmarked], provider="gemini")

    assert "bigco" not in prompt


async def test_an_exchange_with_no_mail_in_it_goes_to_a_remote_model(ask) -> None:
    _, prompt = await ask(
        "which of those are remote?", [{**KAFKA, "mail_in_context": False}], provider="gemini"
    )

    assert KAFKA["answer"] in _history_part(prompt)


async def test_the_reply_says_whether_mail_was_in_its_context(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    """What the dashboard sends back as `mail_in_context` next time. Not
    `shared_mail`, which is true of every local answer: mail is only read for
    an application, and on the chat page there is none."""
    import apps.api.routers.chat as chat_module

    recorder, _ = _answering()
    monkeypatch.setattr(chat_module.llm_router, "build_provider", lambda name=None: recorder())
    application_id = await _application_with_mail(worker_session)

    about_one = await client.post(
        "/chat", json={"message": "any replies?", "application_id": application_id}
    )
    about_none = await client.post("/chat", json={"message": "what needs me?"})

    assert about_one.json()["mail_in_context"] is True
    assert about_none.json()["mail_in_context"] is False
    assert about_none.json()["shared_mail"] is True


# --------------------------------------------------------------------------
# The dashboard
# --------------------------------------------------------------------------

WEB = Path(__file__).resolve().parents[1] / "apps" / "web" / "src"


def test_the_dock_sends_what_it_remembers_and_can_forget() -> None:
    dock = (WEB / "components" / "assistant.tsx").read_text()

    assert "history: historyFrom(" in dock
    assert "New conversation" in dock


def test_the_dock_sends_only_finished_answers() -> None:
    """A refusal, a crawl command and an error are turns on screen and are not
    exchanges: no model answered them."""
    remembered = (WEB / "lib" / "chat-history.ts").read_text()

    for skipped in ('"refused"', '"crawler"'):
        assert skipped in remembered
    assert "mail_in_context" in remembered and "cited" in remembered


# --------------------------------------------------------------------------
# The stream
# --------------------------------------------------------------------------


async def test_the_streamed_route_remembers_the_same_way(client: AsyncClient, monkeypatch) -> None:
    """Both routes run `prepare`, so they cannot remember differently. Asked
    through the stream to hold that, since the dock asks nowhere else."""
    import json

    import apps.api.routers.chat as chat_module

    recorder, seen = _answering()
    monkeypatch.setattr(chat_module.llm_router, "build_provider", lambda name=None: recorder())

    response = await client.post(
        "/chat/stream", json={"message": "which of those are remote?", "history": [KAFKA]}
    )

    assert response.status_code == 200, response.text
    closing = json.loads(response.text.strip().splitlines()[-1])
    assert closing["type"] == "done" and closing["reply"]["history_used"] == 1
    assert KAFKA["answer"] in _history_part(seen["user"])
