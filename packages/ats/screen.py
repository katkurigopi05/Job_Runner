"""Read the form's questions before answering any of them.

The pipeline fills what it can and parks whatever it cannot at
`needs_review` (§2.4). That is the right handling and it happens at the wrong
moment for one class of question: the ones where *answering correctly* is
what ends the application, and the ones an employer should not be asking.

Both are visible in `enumerate_fields()` output, before a single field is
filled and before a screenshot is worth taking. Surfacing them there costs
nothing and gives the owner the decision while it is still theirs to make.

## Two kinds of finding, deliberately kept apart

A **knock-out** is a question whose truthful answer, from this profile, is
likely disqualifying — sponsorship on a posting that cannot sponsor, a
clearance the owner does not hold. Nothing is wrong with the question. The
point is to say so before the effort is spent, not after a rejection.

A **caution** is a question that is unlawful to ask in some jurisdictions, or
that §2.2 protects. Salary history is prohibited in several US states; date of
birth and marital status are prohibited in most places to ask before an offer.

## What this deliberately does not do

It does not answer, skip, or fail anything. §2.2 still copies
work-authorization answers verbatim from the profile, and §2.4 still parks
anything unanswerable. A caution never changes what happens at all — the
owner may well decide a prohibited-sounding question is a badly worded one
and answer it anyway.

A knock-out does one thing: it parks the run on the *unattended* path
(`apply_job._decide`). Auto-submitting into a question this profile visibly
fails costs the owner nothing and a recruiter their time, which is the spray
behaviour this project exists not to do. It never overrides an approval the
owner has already given — that branch runs first and stands on its own.

It also never asserts anyone is breaking the law. A finding states what the
question asks and where that is restricted; exemptions and context are not
knowable from a form field.

The pre-answer screen is an idea from santifer/career-ops (MIT), whose apply
mode warns about knock-out and prohibited questions before the candidate
commits to answering them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

import structlog

from packages.ats.base import Question
from packages.core.models import Profile

log = structlog.get_logger(__name__)


class Finding(StrEnum):
    """Type of issue found in a form question."""

    KNOCK_OUT = "knock_out"
    CAUTION = "caution"


@dataclass(frozen=True)
class ScreenedQuestion:
    """One flagged field. Carries the label verbatim, per §2.4."""

    key: str
    label: str
    finding: Finding
    reason: str


@dataclass
class ScreenReport:
    """Results of screening a form's questions before answering."""

    knock_outs: list[ScreenedQuestion] = field(default_factory=list)
    cautions: list[ScreenedQuestion] = field(default_factory=list)

    @property
    def any_findings(self) -> bool:
        """True if any issues were found."""
        return bool(self.knock_outs or self.cautions)

    def as_dict(self) -> dict[str, object]:
        """Serialize for API response."""

        def render(items: list[ScreenedQuestion]) -> list[dict[str, str]]:
            """Convert screened questions to dicts."""
            return [
                {"key": q.key, "label": q.label, "reason": q.reason, "finding": q.finding.value}
                for q in items
            ]

        return {"knock_outs": render(self.knock_outs), "cautions": render(self.cautions)}


_SPONSORSHIP_RE = re.compile(
    r"\b(sponsor(?:ship)?|visa support|require.{0,20}sponsor|h-?1b transfer)\b", re.I
)
_AUTHORIZATION_RE = re.compile(
    r"\b(legally authoriz|authorized to work|right to work|work permit|work authoriz)\b", re.I
)
_CLEARANCE_RE = re.compile(r"\b(security clearance|ts/sci|top secret|public trust)\b", re.I)
_RELOCATION_RE = re.compile(r"\b(relocat\w*|willing to move)\b", re.I)
_ONSITE_RE = re.compile(r"\b(on-?site|in.?office|commut\w*|hybrid schedule)\b", re.I)

#: Restricted questions, and where. Phrased as what the question asks and
#: where that is limited — never as an accusation.
_CAUTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(current|previous|prior|most recent).{0,20}(salary|compensation|pay)\b", re.I
        ),
        "asks for salary history, which several US states and jurisdictions "
        "prohibit employers from requesting",
    ),
    (
        re.compile(r"\b(date of birth|birth ?date|d\.?o\.?b\.?|your age|age\?)\b", re.I),
        "asks for age or date of birth, which is generally restricted before an offer",
    ),
    (
        re.compile(r"\b(marital status|married|spouse|children|pregnan\w*|dependents)\b", re.I),
        "asks about family or marital status, which is generally restricted",
    ),
    (
        re.compile(r"\b(canadian experience|local experience only)\b", re.I),
        "asks for experience in a specific country, which Ontario restricts",
    ),
    (
        re.compile(
            r"\b(citizen(?:ship)? status|are you a .{0,15}citizen|green card holder)\b", re.I
        ),
        "asks for a specific immigration status rather than whether the "
        "candidate is authorized to work",
    ),
    (
        re.compile(r"\b(religio\w*|ethnicit\w*|national origin)\b", re.I),
        "asks about a protected characteristic outside a voluntary EEO section",
    ),
)

#: Voluntary self-identification sections legitimately ask about protected
#: characteristics. They are optional by construction, and flagging them
#: would fire on nearly every US application until nobody read the warnings.
_VOLUNTARY_RE = re.compile(
    r"\b(voluntar\w*|self.?identif\w*|eeo|equal employment|affirmative action|"
    r"decline to (?:self.?identify|answer)|prefer not to (?:say|answer))\b",
    re.I,
)


def _text_of(question: Question) -> str:
    return f"{question.label} {question.help_text or ''}"


def _knock_out_for(question: Question, profile: Profile) -> ScreenedQuestion | None:
    """A question whose honest answer from this profile likely disqualifies."""
    text = _text_of(question)

    if profile.needs_sponsorship and _SPONSORSHIP_RE.search(text):
        return ScreenedQuestion(
            question.key,
            question.label,
            Finding.KNOCK_OUT,
            "this profile needs sponsorship and the form asks about it",
        )

    if _AUTHORIZATION_RE.search(text) and not (profile.work_auth or "").strip():
        return ScreenedQuestion(
            question.key,
            question.label,
            Finding.KNOCK_OUT,
            "asks about work authorization and the profile records none; §2.2 "
            "forbids generating an answer, so this cannot be filled",
        )

    if _CLEARANCE_RE.search(text):
        return ScreenedQuestion(
            question.key,
            question.label,
            Finding.KNOCK_OUT,
            "asks for a security clearance",
        )

    if question.required and _RELOCATION_RE.search(text):
        return ScreenedQuestion(
            question.key,
            question.label,
            Finding.KNOCK_OUT,
            "requires a relocation commitment",
        )

    if question.required and _ONSITE_RE.search(text) and (profile.location or ""):
        return ScreenedQuestion(
            question.key,
            question.label,
            Finding.KNOCK_OUT,
            f"requires on-site presence; the profile is based in {profile.location}",
        )

    return None


def _caution_for(question: Question) -> ScreenedQuestion | None:
    text = _text_of(question)

    if _VOLUNTARY_RE.search(text):
        return None

    for pattern, reason in _CAUTIONS:
        if pattern.search(text):
            return ScreenedQuestion(question.key, question.label, Finding.CAUTION, reason)
    return None


def screen(questions: list[Question], profile: Profile) -> ScreenReport:
    """Flag knock-out and restricted questions. Answers nothing."""
    report = ScreenReport()

    for question in questions:
        knock_out = _knock_out_for(question, profile)
        if knock_out is not None:
            report.knock_outs.append(knock_out)

        caution = _caution_for(question)
        if caution is not None:
            report.cautions.append(caution)

    if report.any_findings:
        # Labels are the employer's words and go in the review record, not the
        # log — §10 keeps page content out of the logs.
        log.info(
            "form_screened",
            knock_outs=len(report.knock_outs),
            cautions=len(report.cautions),
        )
    return report
