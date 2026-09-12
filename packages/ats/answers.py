"""Map enumerated form questions to profile answers.

Phase 1 does this by deterministic key/label matching only. No LLM is involved
yet — the field-to-profile-key mapper from CLAUDE.md §7 arrives in Phase 2.
Anything not matched here is left unanswered on purpose, which parks the
application rather than guessing (CLAUDE.md §2.4).

Work-authorization answers are copied verbatim from the profile and are never
generated. CLAUDE.md §2.2.
"""

from __future__ import annotations

import re
from typing import Any

from packages.ats.base import Question, QuestionKind, UnsupportedSiteError
from packages.ats.registry import adapter_by_name
from packages.core.models import Candidate, Profile

#: Normalized label fragments → profile attribute. Ordered: first match wins,
#: so put the specific patterns above the general ones.
LABEL_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bfirst\s*name\b"), "first_name"),
    (re.compile(r"\blast\s*name\b|\bsurname\b|\bfamily\s*name\b"), "last_name"),
    (re.compile(r"\bfull\s*name\b|^name$"), "full_name"),
    (re.compile(r"\bemail\b"), "email"),
    (re.compile(r"\bphone\b|\bmobile\b|\btelephone\b"), "phone"),
    (re.compile(r"\blinkedin\b"), "linkedin"),
    (re.compile(r"\bgithub\b"), "github"),
    (re.compile(r"\bportfolio\b|\bpersonal\s*(web)?site\b"), "portfolio"),
    (re.compile(r"\bwebsite\b"), "portfolio"),
    (re.compile(r"\bresume\b|\bcv\b"), "resume"),
    (re.compile(r"\bcover\s*letter\b"), "cover_letter"),
    (re.compile(r"\blocation\b|\bcity\b|\bwhere are you\b"), "location"),
    (
        re.compile(r"\bsponsor|\bvisa\b|\bwork\s*(authoriz|permit)|\blegally\b"),
        "work_auth",
    ),
    (re.compile(r"\bsalary\b|\bcompensation\b|\bpay\s*expect"), "salary_expectation"),
)

#: Keys whose answers come straight from the profile with no transformation.
#: These have legal consequences; never route them through a model.
VERBATIM_KEYS = frozenset({"work_auth", "salary_expectation"})


def _split_name(full: str) -> tuple[str, str]:
    parts = full.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def profile_values(
    candidate: Candidate, profile: Profile, *, reply_to: str | None = None
) -> dict[str, Any]:
    """Flatten a candidate + profile into the vocabulary LABEL_RULES uses.

    `reply_to` is the per-application alias for a candidate in managed email
    mode. It replaces the candidate's own address because the alias only
    works if the employer is given it — see packages/inbox/alias.py.
    """
    first, last = _split_name(candidate.name or "")
    links = profile.links_json or {}

    values: dict[str, Any] = {
        "first_name": first,
        "last_name": last,
        "full_name": candidate.name,
        "email": reply_to or candidate.email,
        "phone": profile.phone,
        "location": profile.location,
        "work_auth": profile.work_auth,
        "salary_expectation": profile.salary_expectation,
        "linkedin": links.get("linkedin"),
        "github": links.get("github"),
        "portfolio": links.get("portfolio") or links.get("website"),
    }
    # Free-form answers the owner has stored take precedence over nothing —
    # they are additional keys, not overrides of the structured fields above.
    for key, value in (profile.answers_kv_json or {}).items():
        values.setdefault(key, value)
    return values


def _match_attribute(question: Question) -> str | None:
    """The profile attribute this question is asking for, by its wording.

    Label first, then the key. Searching them joined defeated every anchored
    rule: `^name$` could never fire, because the haystack was always the label
    *plus* the field name — so Ashby's `name` field, labelled exactly `name`,
    matched nothing at all.

    The label is also the better evidence of the two. It is what the employer
    wrote for a person to read; a field name is an internal handle, and on
    three of the four ATSes it is a uuid.
    """
    for haystack in (question.label.lower(), question.key.lower()):
        for pattern, attribute in LABEL_RULES:
            if pattern.search(haystack):
                return attribute
    return None


def _mapped_attribute(question: Question, ats: str | None) -> str | None:
    """What the ATS itself says this field is for.

    Consulted before the label rules, because it is not a guess: `ashby.py`
    states that `_systemfield_name` is the full name, and that is better
    evidence than any regex over the wording beside it.

    These maps existed in three adapters from the day each was written and had
    no caller outside their own tests. On a live ElevenLabs posting the
    label rules alone filled 1 field of 9.
    """
    if not ats:
        return None
    try:
        adapter = adapter_by_name(ats)
    except UnsupportedSiteError:
        # A stored `ats` string this registry no longer knows must not raise
        # in the middle of an apply; the label rules still work.
        return None
    return adapter.profile_key_for(question.key)


def asks_for_cover_letter(question: Question) -> bool:
    """Does this field want a cover letter?

    Exported so the apply pipeline can decide whether writing one is worth a
    provider call, using the same rule that decides where to put it. Two
    copies of this test would drift, and the direction it drifts matters: a
    letter written and then not recognized is paid for and discarded.
    """
    return _match_attribute(question) == "cover_letter"


def build_answers(
    questions: list[Question],
    candidate: Candidate,
    profile: Profile,
    *,
    extra: dict[str, Any] | None = None,
    resume_path: str | None = None,
    cover_letter_text: str | None = None,
    cover_letter_path: str | None = None,
    reply_to: str | None = None,
    ats: str | None = None,
) -> dict[str, Any]:
    """Answers keyed by `Question.key`, for `ATSAdapter.fill()`.

    `extra` carries answers the owner supplied at review; they win over
    anything derived from the profile.

    The two cover-letter parameters are the same letter in the two shapes an
    employer asks for it. Greenhouse offers "Attach" and "Enter manually" for
    the same field, so both can appear on one form; each takes the shape it
    can accept, and a form that offers neither gets nothing rather than a
    path typed into a textarea.

    `reply_to` overrides the email field with this application's alias, so
    the employer's reply comes back carrying an exact key. None applies with
    the candidate's own address.

    `ats` lets the adapter's own field-name map answer first. Omitted, this
    behaves exactly as it did before: label rules only.
    """
    values = profile_values(candidate, profile, reply_to=reply_to)
    owner_supplied = extra or {}
    answers: dict[str, Any] = {}

    for question in questions:
        # An owner-supplied answer for this exact field wins — but an empty one
        # is not an answer. Letting a null shadow a real value would mean a
        # review that omitted a field silently unset the résumé attached to it.
        if question.key in owner_supplied and owner_supplied[question.key] not in (None, ""):
            answers[question.key] = owner_supplied[question.key]
            continue

        attribute = _mapped_attribute(question, ats) or _match_attribute(question)
        if attribute is None:
            continue

        if attribute == "resume":
            if resume_path and question.kind is QuestionKind.FILE:
                answers[question.key] = resume_path
            continue

        if attribute == "cover_letter":
            # Written by packages/tailor/cover.py and vetted by the fabrication
            # guard before it reaches here. Still unanswered when there is no
            # letter — the guard refuses rather than falling back, and an empty
            # field the owner sees at review beats one filled with something
            # invented (CLAUDE.md §2.4).
            if question.kind is QuestionKind.FILE:
                if cover_letter_path:
                    answers[question.key] = cover_letter_path
            elif cover_letter_text:
                answers[question.key] = cover_letter_text
            continue

        value = values.get(attribute)
        if value in (None, ""):
            continue

        # A yes/no control cannot take free text; leaving it unanswered sends
        # the exact question to the owner instead of coercing a guess.
        if question.kind in (
            QuestionKind.SINGLE_SELECT,
            QuestionKind.MULTI_SELECT,
            QuestionKind.RADIO,
        ):
            chosen = _match_option(question, value)
            if chosen is None:
                continue
            answers[question.key] = chosen
            continue

        answers[question.key] = value

    return answers


def _match_option(question: Question, value: Any) -> str | None:
    """Find an option that genuinely matches the profile value.

    Exact label or value match only. Fuzzy matching here would mean silently
    answering a work-authorization question with something the owner never
    said, which is exactly the failure mode §2.2 exists to prevent.
    """
    text = str(value).strip().lower()
    for option in question.options:
        if option.value.lower() == text or option.label.strip().lower() == text:
            return option.value
    return None
