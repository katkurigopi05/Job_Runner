"""Read a real mail export into a labeling worksheet.

Gate 6 asks for "30 hand-labeled **real** recruiter emails". There are 30
written to match, and `inbound_messages` in the owner's database is 0 — so the
number the gate reports has never been computed against a real message. That
is not a gap a better fixture closes; it needs the owner's actual mail.

This is the path from one to the other. Point it at a Gmail/Thunderbird
`.mbox` or a directory of `.eml` files and it produces a worksheet: one row
per message, with the classifier's current guess beside an **empty** label
field. The owner fills the labels in; scoring reads only the rows they filled.

## The guess is never the label

`guess` is written for convenience — most rows will be right and confirming is
faster than typing. It is deliberately a *separate field* from `label`, and a
row with no `label` is excluded from scoring entirely.

Collapsing the two would make the classifier grade its own homework, which is
exactly the self-evaluation CLAUDE.md §45 and the master spec both refuse. The
resulting accuracy would be 100% by construction and would mean nothing.

## What is deliberately not stored

Only what a classification decision needs: sender, subject, body, date. No
attachments, no recipient list beyond the delivered-to alias that routes a
message to an application. Recruiter mail is other people's writing about the
owner (CLAUDE.md §14), and a worksheet is a second copy of it on disk — so it
holds the minimum that makes the measurement possible.
"""

from __future__ import annotations

import mailbox
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from packages.core.enums import Classification
from packages.inbox.imap import parse_message
from packages.inbox.route import InboundEmail

#: What Gate 6 asks for. Reported, never enforced — a worksheet of twelve is
#: still worth more than thirty fixtures.
GATE_6_TARGET = 30


@dataclass
class ImportReport:
    files: int = 0
    messages: int = 0
    skipped: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"{self.messages} messages from {self.files} file(s)"]
        for reason in self.skipped[:5]:
            lines.append(f"  skipped: {reason}")
        if len(self.skipped) > 5:
            lines.append(f"  ... and {len(self.skipped) - 5} more skipped")
        if self.messages < GATE_6_TARGET:
            lines.append(
                f"  Gate 6 asks for {GATE_6_TARGET}; this is {self.messages}. "
                "Still worth scoring — just say which number it is."
            )
        return "\n".join(lines)


def read_export(path: Path) -> tuple[list[InboundEmail], ImportReport]:
    """Parse an `.mbox`, an `.eml`, or a directory of either."""
    report = ImportReport()
    messages: list[InboundEmail] = []

    for source in _sources(path):
        report.files += 1
        try:
            if source.suffix.lower() == ".mbox":
                for raw in mailbox.mbox(str(source)):
                    messages.append(parse_message(raw.as_bytes()))
            else:
                messages.append(parse_message(source.read_bytes()))
        except Exception as exc:  # noqa: BLE001 — one bad file must not stop the import
            report.skipped.append(f"{source.name}: {type(exc).__name__}: {exc}")

    report.messages = len(messages)
    return messages, report


def _sources(path: Path) -> list[Path]:
    if path.is_dir():
        return sorted(
            p for p in path.rglob("*") if p.suffix.lower() in {".eml", ".mbox"} and p.is_file()
        )
    return [path]


def build_worksheet(messages: list[InboundEmail], guesses: list[Classification | None]) -> dict:
    """A worksheet the owner fills in. `label` starts empty on every row."""
    return {
        "note": (
            "Fill in `label` on each row with one of: "
            + ", ".join(c.value for c in Classification)
            + ". `guess` is what the classifier currently says — it is NOT the "
            "answer, and rows left unlabeled are skipped when scoring."
        ),
        "messages": [
            {
                "message_id": message.message_id,
                "from": message.from_addr,
                "delivered_to": message.delivered_to,
                "subject": message.subject,
                "received_at": (message.received_at.isoformat() if message.received_at else None),
                "body": message.body,
                "guess": guess.value if guess else None,
                "label": None,
            }
            for message, guess in zip(messages, guesses, strict=True)
        ],
    }


@dataclass
class ScoreReport:
    labeled: int = 0
    correct: int = 0
    wrong: list[tuple[str, str, str]] = field(default_factory=list)
    unlabeled: int = 0

    @property
    def accuracy(self) -> float:
        return self.correct / self.labeled if self.labeled else 0.0

    def summary(self) -> str:
        if not self.labeled:
            return (
                "No rows carry a `label`. Fill some in — the guesses are not "
                "labels, and scoring against them would measure nothing."
            )
        lines = [
            f"{self.correct}/{self.labeled} correct "
            f"({self.accuracy:.1%}) on real mail; {self.unlabeled} unlabeled rows skipped"
        ]
        for subject, want, got in self.wrong[:10]:
            lines.append(f"  want {want:<12} got {got:<12} {subject[:56]}")
        if self.labeled < GATE_6_TARGET:
            lines.append(
                f"  Gate 6 asks for {GATE_6_TARGET} labeled messages; this is "
                f"{self.labeled}. The number is real either way — just quote the n."
            )
        return "\n".join(lines)


def score_worksheet(worksheet: dict, classify: object) -> ScoreReport:
    """Re-classify every labeled row and compare. `classify(subject, body)`."""
    report = ScoreReport()
    for row in worksheet.get("messages") or []:
        want_raw = row.get("label")
        if not want_raw:
            report.unlabeled += 1
            continue
        want = Classification(want_raw)
        got = classify(row.get("subject") or "", row.get("body") or "")  # type: ignore[operator]
        report.labeled += 1
        if got is want:
            report.correct += 1
        else:
            report.wrong.append((row.get("subject") or "", want.value, got.value))
    return report


def write_worksheet(worksheet: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(worksheet, sort_keys=False, allow_unicode=True))
    return path


def read_worksheet(path: Path) -> dict:
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict) or "messages" not in raw:
        raise ValueError(f"{path}: expected a mapping with a `messages:` list")
    return raw
