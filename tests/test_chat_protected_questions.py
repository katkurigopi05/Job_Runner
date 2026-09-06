"""§2.2, asked in a sentence rather than named as a field.

`router.is_protected` reads an ATS field name — it matches
`work_authorization_status` and `question_12074270004` by substring. `/chat`
reused it on natural language, where only the literal token
`salary_expectation` was listed. Measured against the live route: 2 of 5
salary phrasings were refused and the rest went to the model, while the
refusal's own text says it covers salary.

§14 names that outcome the §2.2 failure by example — a model asked what to
earn "advised on how to research one" instead of pointing at the profile.
"""

from __future__ import annotations

import pytest

from apps.api.routers.chat import asks_for_a_protected_answer

#: The owner asking what *they* should put. All of these reach a real form.
REFUSE = (
    "What salary should I ask for?",
    "How much should I say I want to be paid?",
    "What is my expected compensation?",
    "What should I put for salary expectation?",
    "Do I need sponsorship?",
    "What should I put for my work authorization?",
    "Am I authorized to work in the US?",
    "What is my employment history?",
    "Should I say I need a visa?",
)

#: Questions the assistant is *supposed* to answer from what it was handed.
#: Refusing these would break a real feature to protect nothing — the number
#: belongs to the posting, not to the owner.
ANSWER = (
    "What salary is this posting offering?",
    "Which companies pay the most?",
    "How many applications are waiting?",
    "What is the status of my Acme application?",
    "Show me the sponsorship policy in this posting",
)


@pytest.mark.parametrize("question", REFUSE)
def test_the_owner_asking_what_to_put_is_refused(question: str) -> None:
    assert asks_for_a_protected_answer(question), question


@pytest.mark.parametrize("question", ANSWER)
def test_a_question_about_the_data_is_still_answered(question: str) -> None:
    assert not asks_for_a_protected_answer(question), question


def test_a_protected_topic_alone_is_not_enough() -> None:
    """The first-person requirement is what keeps the posting case answerable.

    Losing it would refuse "what salary is this posting offering", which is
    grounded data and the whole point of the assistant.
    """
    assert not asks_for_a_protected_answer("salary")
    assert not asks_for_a_protected_answer("sponsorship requirements")
    assert asks_for_a_protected_answer("my salary")
