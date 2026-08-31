"""Importing real mail so Gate 6 can be measured on it.

Gate 6 asks for 30 hand-labeled **real** recruiter emails. There are 30 written
to match, and they were written beside the patterns that read them, so they use
the phrasing the patterns expect. `inbound_messages` is 0: the number the gate
reports has never been computed against a message a recruiter actually sent.

These tests exercise the path from a mail export to that number. The messages
here are constructed RFC-822 bytes — this proves the plumbing, not the gate.
The gate's own number still waits on the owner's mailbox.
"""

from __future__ import annotations

import mailbox
from email.message import EmailMessage
from pathlib import Path

import pytest
import yaml

from packages.core.enums import Classification
from packages.inbox.importer import (
    GATE_6_TARGET,
    build_worksheet,
    read_export,
    read_worksheet,
    score_worksheet,
    write_worksheet,
)


def _eml(subject: str, body: str, sender: str = "dana@acme.example") -> bytes:
    message = EmailMessage()
    message["Message-ID"] = f"<{abs(hash(subject))}@acme.example>"
    message["From"] = sender
    message["To"] = "owner+app1@gmail.com"
    message["Subject"] = subject
    message.set_content(body)
    return message.as_bytes()


@pytest.fixture
def export(tmp_path: Path) -> Path:
    folder = tmp_path / "mail"
    folder.mkdir()
    (folder / "a.eml").write_bytes(
        _eml("Re: Senior Backend Engineer", "We've decided to move forward with another candidate.")
    )
    (folder / "b.eml").write_bytes(
        _eml("Interview invitation", "We would like to invite you to a technical interview.")
    )
    return folder


def test_a_directory_of_eml_is_read(export: Path) -> None:
    messages, report = read_export(export)

    assert report.messages == 2
    assert report.files == 2
    assert {m.subject for m in messages} == {
        "Re: Senior Backend Engineer",
        "Interview invitation",
    }
    assert any("move forward with another candidate" in m.body for m in messages)


def test_an_mbox_is_read(tmp_path: Path) -> None:
    path = tmp_path / "mail.mbox"
    box = mailbox.mbox(str(path))
    box.add(_eml("Re: your application", "Unfortunately we will not be moving forward."))
    box.flush()
    box.close()

    messages, report = read_export(path)

    assert report.messages == 1
    assert "not be moving forward" in messages[0].body


def test_one_unreadable_file_does_not_stop_the_import(export: Path) -> None:
    """A half-finished import is worth more than an exception."""
    (export / "broken.mbox").write_bytes(b"\x00\x01 not a mailbox")

    _, report = read_export(export)

    assert report.messages == 2, "the two good messages still arrive"


def test_the_worksheet_leaves_every_label_empty(export: Path) -> None:
    """The guess must never be the answer.

    Filling `label` from the classifier would make it grade its own homework:
    accuracy would be 100% by construction. §45 and the master spec both
    refuse a self-grading evaluator, and this is where that would sneak in.
    """
    messages, _ = read_export(export)
    sheet = build_worksheet(messages, [Classification.REJECTION, Classification.INTERVIEW])

    assert [row["label"] for row in sheet["messages"]] == [None, None]
    assert [row["guess"] for row in sheet["messages"]] == ["rejection", "interview"]


def test_unlabeled_rows_are_skipped_not_counted_wrong(export: Path, tmp_path: Path) -> None:
    messages, _ = read_export(export)
    sheet = build_worksheet(messages, [None, None])
    sheet["messages"][0]["label"] = "rejection"

    report = score_worksheet(sheet, lambda s, b: Classification.REJECTION)

    assert report.labeled == 1
    assert report.correct == 1
    assert report.unlabeled == 1


def test_scoring_an_entirely_unlabeled_sheet_says_so(export: Path) -> None:
    """Reporting 0% would read as a broken classifier rather than no labels."""
    messages, _ = read_export(export)
    sheet = build_worksheet(messages, [Classification.REJECTION, Classification.INTERVIEW])

    report = score_worksheet(sheet, lambda s, b: Classification.REJECTION)

    assert report.labeled == 0
    assert "not labels" in report.summary()


def test_the_worksheet_round_trips(export: Path, tmp_path: Path) -> None:
    messages, _ = read_export(export)
    sheet = build_worksheet(messages, [None, None])
    sheet["messages"][0]["label"] = "rejection"
    sheet["messages"][1]["label"] = "interview"

    path = write_worksheet(sheet, tmp_path / "labels.yaml")
    reloaded = read_worksheet(path)

    assert [r["label"] for r in reloaded["messages"]] == ["rejection", "interview"]
    assert yaml.safe_load(path.read_text())["messages"][0]["subject"]


def test_the_real_rules_score_a_labeled_sheet(export: Path) -> None:
    """End to end, through the classifier the inbox actually uses."""
    from packages.inbox.classify import RuleClassifier

    def classify(subject: str, body: str) -> Classification:
        verdict = RuleClassifier().classify(subject, body)
        return Classification.NOISE if verdict.abstained else verdict.classification

    messages, _ = read_export(export)
    sheet = build_worksheet(messages, [None, None])
    for row in sheet["messages"]:
        row["label"] = "rejection" if "Senior Backend" in row["subject"] else "interview"

    report = score_worksheet(sheet, classify)

    assert report.labeled == 2
    assert report.correct == 2, report.summary()


def test_a_short_sheet_says_how_short(export: Path) -> None:
    """Two labels is a real number; it is just not thirty."""
    messages, _ = read_export(export)
    sheet = build_worksheet(messages, [None, None])
    sheet["messages"][0]["label"] = "rejection"

    summary = score_worksheet(sheet, lambda s, b: Classification.REJECTION).summary()

    assert str(GATE_6_TARGET) in summary
