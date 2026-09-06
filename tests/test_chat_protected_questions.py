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

from apps.api.routers.chat import asks_for_a_protected_answer, names_a_protected_field
from packages.llm import router as llm_router

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
    # Plural is the same question. Under-refusing is the direction with
    # consequences, and nothing that must stay answerable says "we".
    "What salary should we ask for?",
    "Should we say we need a visa?",
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
    """Every one of these ends up on a real application under the owner's name.

    §2.2 makes them profile values copied word for word, so the assistant must
    not draft one — and the refusal runs in code rather than in the prompt,
    because a prompt is a request and this is a rule.
    """
    assert asks_for_a_protected_answer(question), question


@pytest.mark.parametrize("question", ANSWER)
def test_a_question_about_the_data_is_still_answered(question: str) -> None:
    """The other half, and the one a widening breaks first.

    These ask about a posting or the queue, not about what to put on a form.
    Refusing them would trade a real feature for no protection at all, so the
    set is parametrised: a future tightening that swallows one fails here
    rather than passing quietly.
    """
    assert not asks_for_a_protected_answer(question), question


def test_a_protected_topic_alone_is_not_enough() -> None:
    """The first-person requirement is what keeps the posting case answerable.

    Losing it would refuse "what salary is this posting offering", which is
    grounded data and the whole point of the assistant.
    """
    assert not asks_for_a_protected_answer("salary")
    assert not asks_for_a_protected_answer("sponsorship requirements")
    assert asks_for_a_protected_answer("my salary")


def _route_refuses(question: str) -> bool:
    """The route's whole condition, as `chat()` evaluates it."""
    return names_a_protected_field(question) or asks_for_a_protected_answer(question)


class TestTheFieldMatcherIsNotRunOverProse:
    """`is_protected` matches an ATS field name by substring.

    Run over a sentence it fires on any prose containing one, so "What salary
    expectation does this posting list?" was refused — a question about the
    posting's advertised pay, which is grounded data and the point of the
    assistant. It is kept only for a message that is a field name.
    """

    @pytest.mark.parametrize(
        "question",
        (
            "What salary expectation does this posting list?",
            "Does this posting list a salary expectation?",
            # No question mark and only three words, which the first version of
            # the gate read as a field name and refused.
            "salary expectation listed",
            "salary expectation in this posting",
        ),
    )
    def test_prose_containing_a_field_name_still_reaches_the_model(self, question: str) -> None:
        """The precondition is half the test.

        Asserting `is_protected` really does fire on the sentence is what makes
        the second line meaningful: without it the test would still pass if the
        matcher simply stopped matching, and the gate it exists to pin would be
        gone unnoticed.
        """
        assert llm_router.is_protected(question), "precondition: the matcher does fire on it"
        assert not _route_refuses(question)

    @pytest.mark.parametrize("field", ("work_authorization", "salary_expectation", "sponsorship"))
    def test_a_bare_field_name_is_still_refused(self, field: str) -> None:
        """Pasting the field into the box means the field."""
        assert _route_refuses(field)

    @pytest.mark.parametrize(
        "label",
        (
            "work_authorization",
            "work authorization",
            "employment_history",
            "employment history",
            "work history",
            "salary_expectation",
            "salary expectation",
            "sponsorship",
            "needs_sponsorship",
            "needs sponsorship",
            # An ATS names the same question differently every other week.
            "work_authorization_status",
            # Every spelling of one label is one message. Branching on shape
            # first is what let these through: `work history` was checked
            # against the topic list and `work_history` was not.
            "work_history",
            "work-history",
            "employment_history",
            "salary-expectation",
        ),
    )
    def test_every_supported_label_is_refused_however_it_is_spelled(self, label: str) -> None:
        """One token or several, underscored or spaced — all the same message.

        Requiring one token was a §2.2 *under*-refusal: `is_protected`
        normalises the space and does know `employment history`, but the
        one-token gate meant it was never asked, so a protected label reached
        the model. That is the direction with consequences.
        """
        assert _route_refuses(label), label

    def test_a_label_is_the_whole_message_or_it_is_prose(self) -> None:
        """Exact, never a substring.

        Counting words and looking for a question mark was too loose in the
        direction that costs a feature: any short phrase containing a field
        name read as one. Matching a multiword label by substring instead of
        exactly would bring that straight back.
        """
        assert names_a_protected_field("work_authorization")
        assert names_a_protected_field("employment history"), "an exact label, spaced"
        assert names_a_protected_field("work_history"), "the same label, underscored"
        assert names_a_protected_field("work-history"), "the same label, hyphenated"
        assert not names_a_protected_field("salary expectation listed")
        assert not names_a_protected_field("salary expectation in this posting")
        assert not names_a_protected_field("")
