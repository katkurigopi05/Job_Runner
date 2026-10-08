"""Which questions §2.2 keeps away from a model.

Work authorization, sponsorship, employment history and salary are copied
word for word from the profile, because a wrong one on a real application has
legal consequences. Asked of the assistant, such a question is refused in
code before any model is reached.

Moved out of `routers/chat.py` when the assistant began to remember a
conversation: `chat_history` needs the same reading of a question, to keep
the rule from being walked round in two messages.
"""

from __future__ import annotations

import re

from packages.llm import router as llm_router

#: Topics §2.2 keeps verbatim, as a person says them rather than as an ATS
#: names a field. `router.is_protected` covers the field-name side — it matches
#: `work_authorization_status` and `question_12074270004` by substring — and
#: reusing it on a sentence is what left a hole: only the literal token
#: `salary_expectation` was listed, so "What should I put for salary
#: expectation?" was refused and "What salary should I ask for?" went to the
#: model. Measured at 2 of 5 natural phrasings caught.
#:
#: §14 already names that outcome the §2.2 failure: a model asked what to earn
#: "advised on how to research one" instead of pointing at the profile.
_PROTECTED_TOPICS = (
    "salary",
    "compensation",
    "how much should i",
    "paid",
    "work auth",
    "authorized to work",
    "authorised to work",
    "right to work",
    "sponsor",
    "visa",
    "employment history",
    "work history",
)

#: A protected topic alone is not enough. "What salary is this posting
#: offering?" is a question about a posting, and the assistant is *supposed* to
#: answer that from the data it was handed — refusing it would break a real
#: feature to protect nothing.
#:
#: What makes it a §2.2 question is the owner asking what *they* should say, so
#: the topic has to arrive with a first-person reference. The tradeoff is
#: deliberate and lands on the safe side: "what salary do my matches offer" is
#: refused too, because a false refusal costs one rephrase and a false answer
#: goes onto a real application.
#: "me" is deliberately absent. "Show me the sponsorship policy in this
#: posting" is a request to read the data, not a request to answer for the
#: owner, and it was the one false refusal the test set found. Every phrasing
#: that *is* a §2.2 question carries "I" or "my" instead.
#: Plural included: "what salary should we ask for" is the same question, and
#: under-refusing is the direction with consequences. None of the questions
#: that must stay answerable are phrased with "we" — they say "this posting".
#: The same topics as bare labels, normalised the way `is_protected` does it,
#: so `work history`, `work_history` and `work-history` are one message.
_PROTECTED_LABELS = frozenset(topic.replace(" ", "_") for topic in _PROTECTED_TOPICS)

_FIRST_PERSON = re.compile(r"\b(i|i'm|im|my|mine|myself|we|we're|our|ours|ourselves)\b")


def names_a_protected_field(question: str) -> bool:
    """Whether the whole message *is* a protected field, rather than a sentence.

    `router.is_protected` matches an ATS field name by substring, and run over
    prose it fires on any sentence containing one. "What salary expectation
    does this posting list?" normalises to text containing `salary_expectation`
    and was refused — a question about the *posting's* advertised pay, which is
    grounded data the assistant exists to answer.

    Someone pasting a bare `work_authorization` into the box still means the
    field, though, so the matcher is kept for that and only that.

    Two shapes count. One token is what an ATS emits —  `work_authorization`,
    `question_12074270004` — and a bare protected label written as a person
    writes it, `employment history`, is the same message with a space in it.

    Counting words and looking for a question mark was the first attempt and
    too loose: "salary expectation listed" is three words with no `?`, so it
    was read as a field name and refused, which is a posting question again.
    Requiring one token was the second and too tight in the direction that
    matters — `is_protected` normalises the space and does recognise
    `employment history`, but it was never reached, so a §2.2 label went
    to the model.

    So the multiword case is an *exact* match against the protected
    vocabulary, never a substring: "salary expectation listed" is not a label
    and stays answerable.
    """
    text = question.strip().lower()
    if not text:
        return False

    # Normalise once, then ask every vocabulary. Branching on shape first is
    # what produced three rounds of this: each branch knew a different list,
    # so a label fell between them every time. `work history` was not in
    # `PROTECTED_FIELDS`, and `work_history` was not reached by the topic
    # check, and both went to the model.
    normalized = text.replace("-", "_").replace(" ", "_")
    if normalized in llm_router.PROTECTED_FIELDS or normalized in _PROTECTED_LABELS:
        return True

    # Only a single token falls through to the substring matcher, which is how
    # the ATS variants (`work_authorization_status`) are caught. Prose must not
    # reach it: "what salary expectation does this posting list?" contains a
    # field name and is a question about the posting.
    return len(text.split()) == 1 and llm_router.is_protected(text)


def asks_for_a_protected_answer(question: str) -> bool:
    """Whether this is the owner asking what to put for a §2.2 field.

    Separate from `router.is_protected` on purpose: that one reads a field
    name, this one reads a sentence, and the two want different rules. Folding
    the sentence cases into `PROTECTED_FIELDS` would also protect an ATS field
    called `salary_offered`, which is the posting's number and not the owner's.
    """
    text = question.strip().lower()
    if not _FIRST_PERSON.search(text):
        return False
    return any(topic in text for topic in _PROTECTED_TOPICS)


def is_first_person(text: str) -> bool:
    """Whether the owner is asking about themselves, as the rule above reads it."""
    return bool(_FIRST_PERSON.search(text.strip().lower()))


def touches_a_protected_topic(text: str) -> bool:
    """Whether the text mentions one of the topics at all, in any voice.

    Wider than a refusal on purpose. "What salary does this posting list?" is
    answered, and it still touches the topic.
    """
    lowered = text.lower()
    return any(topic in lowered for topic in _PROTECTED_TOPICS)


def is_refused(question: str) -> bool:
    """The whole of the rule, as `/chat` applies it to a message."""
    return names_a_protected_field(question) or asks_for_a_protected_answer(question)
