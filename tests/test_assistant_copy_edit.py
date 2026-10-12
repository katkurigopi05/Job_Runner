"""A turn in the assistant can be copied, and a question edited and asked again.

Asked for by the owner on 2026-10-12, after typing one question three ways to
get round a wrong answer. Each retyping added two turns to a conversation that
goes back with every later question (`ChatRequest.history`), so the wrong
answer was read as context for the next one, and the third answer repeated the
first word for word.

The dashboard has no JavaScript test runner, so, as in
`test_assistant_dock_streams.py`, this reads the source. What it holds is the
wiring a refactor could undo with nothing else failing. The behaviour was
watched in a browser, with the model's answer stubbed at the network.
"""

from __future__ import annotations

from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "apps/web/src"
ASSISTANT = (WEB / "components/assistant.tsx").read_text(encoding="utf-8")
ACTIONS = (WEB / "components/assistant-turn-actions.tsx").read_text(encoding="utf-8")


def test_an_edited_question_continues_only_what_came_before_it() -> None:
    """The point of editing. Sent with the whole conversation, the answer it
    replaces would go back to the model as something already said."""
    assert "onSend={(text) => void send(text, turns.slice(0, index))}" in ASSISTANT
    assert "async function send(text: string, earlier: Turn[] = turns)" in ASSISTANT
    assert "history: historyFrom(earlier)" in ASSISTANT


def test_the_turns_below_an_edited_question_leave_the_screen_with_it() -> None:
    """What is on screen is what is remembered: the dock's turns are the memory."""
    assert 'setTurns([...earlier, { role: "you", text: question }])' in ASSISTANT


def test_the_editor_says_what_asking_again_removes() -> None:
    """Nothing is kept anywhere else, so the turns below are gone for good."""
    assert "following={turns.length - index - 1}" in ASSISTANT
    assert "Asking again removes the" in ACTIONS


def test_a_question_cannot_be_edited_while_an_answer_is_being_written() -> None:
    """`send` refuses while busy, so an editor open then would take a question
    and do nothing with it."""
    assert "canEdit={!busy}" in ASSISTANT
    assert "disabled={!canEdit}" in ACTIONS


def test_only_a_question_can_be_edited() -> None:
    """An answer is the model's. Editing it would put words in its mouth and
    send them back as history."""
    assert 'role === "you" ? (' in ACTIONS
    assert ASSISTANT.count("<EditQuestion") == 1


def test_escape_closes_the_editor_and_not_the_dock() -> None:
    """The dock listens for Escape on the window and closes itself."""
    escape = ACTIONS.index('event.key === "Escape"')
    assert "event.stopPropagation();" in ACTIONS[escape : escape + 200]


def test_enter_does_not_send_a_word_still_being_composed() -> None:
    """An input method confirms a candidate with Enter."""
    assert "!event.nativeEvent.isComposing" in ACTIONS


def test_copying_says_when_it_could_not() -> None:
    """The clipboard is absent when the dashboard is opened by a LAN address."""
    assert "await navigator.clipboard.writeText(text);" in ACTIONS
    assert 'failed: "copy failed"' in ACTIONS
    assert "could not copy" in ASSISTANT


def test_the_copy_is_announced_where_a_screen_reader_hears_it() -> None:
    """The label changing from "copy" to "copied" is silent. The status line is not."""
    assert "onCopied={(succeeded) =>" in ASSISTANT
    assert 'role="status"' in ASSISTANT


def test_a_new_conversation_closes_an_open_editor() -> None:
    """Its index would point into a conversation that is no longer there."""
    cleared = ASSISTANT.index("setTurns([]);")
    assert "setEditing(null);" in ASSISTANT[cleared : cleared + 80]


def test_the_assistant_stays_within_the_size_a_file_is_kept_to() -> None:
    """The actions live in a file of their own because this one is nearly full."""
    assert len(ASSISTANT.splitlines()) <= 800
