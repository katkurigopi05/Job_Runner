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

from packages.ats.base import Question, QuestionKind
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


def profile_values(candidate: Candidate, profile: Profile) -> dict[str, Any]:
    """Flatten a candidate + profile into the vocabulary LABEL_RULES uses."""
    first, last = _split_name(candidate.name or "")
    links = profile.links_json or {}

    values: dict[str, Any] = {
        "first_name": first,
        "last_name": last,
        "full_name": candidate.name,
        "email": candidate.email,
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
    haystack = f"{question.label} {question.key}".lower()
    for pattern, attribute in LABEL_RULES:
        if pattern.search(haystack):
            return attribute
    return None


def build_answers(
    questions: list[Question],
    candidate: Candidate,
    profile: Profile,
    *,
    extra: dict[str, Any] | None = None,
    resume_path: str | None = None,
    cover_letter: str | None = None,
) -> dict[str, Any]:
    """Answers keyed by `Question.key`, for `ATSAdapter.fill()`.

    `extra` carries answers the owner supplied at review; they win over
    anything derived from the profile.
    """
    values = profile_values(candidate, profile)
    owner_supplied = extra or {}
    answers: dict[str, Any] = {}

    for question in questions:
        # An owner-supplied answer for this exact field wins — but an empty one
        # is not an answer. Letting a null shadow a real value would mean a
        # review that omitted a field silently unset the résumé attached to it.
        if question.key in owner_supplied and owner_supplied[question.key] not in (None, ""):
            answers[question.key] = owner_supplied[question.key]
            continue

        attribute = _match_attribute(question)
        if attribute is None:
            continue

        if attribute == "resume":
            if resume_path and question.kind is QuestionKind.FILE:
                answers[question.key] = resume_path
            continue

        if attribute == "cover_letter":
            # Supplied by the caller when one was written and vetted. Absent,
            # it stays unanswered rather than being filled with something
            # invented — §2.4, and the reason this never became a required
            # step: a letter the guard refused is no letter at all.
            if cover_letter:
                answers[question.key] = cover_letter
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
