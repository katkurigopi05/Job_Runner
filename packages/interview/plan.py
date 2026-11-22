"""Conservative local prep and coverage from saved, attributable excerpts."""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from typing import Any
from uuid import UUID

from packages.interview.schema import (
    Coverage,
    Details,
    Evidence,
    Plan,
    PracticeBlock,
    Requirement,
    Snapshot,
    SourceKind,
    TopicPlan,
)
from packages.matching.experience import Demand, heading_kind
from packages.matching.requirements import extract
from packages.matching.skill_vocab import find_skills

CANDIDATE_KINDS = {"resume", "coursework", "internship", "project"}
RUBRIC = (
    "Relevance to the question",
    "Concrete evidence and your own contribution",
    "Reasoning, trade-offs, and validation",
)
ROLE_FOCUS = (
    (r"agentic|agent engineer", "Explain how you evaluate tool calls and handle a failed step."),
    (r"applied ai", "Explain an AI feature's evaluation and what you would measure with users."),
    (r"\b(ai|ml)\b|machine learning", "Walk through a baseline, error analysis, and monitoring."),
    (r"software|developer", "Explain an interface, its failure modes, and how you tested it."),
)
GENERAL_PRACTICE = "General practice suggestion."
PRODUCTION = re.compile(r"\b(production|at scale|on.call|commercial|professional)\b", re.I)


def key(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


def evidence(kind: SourceKind, reference: str, excerpt: str, **metadata: Any) -> Evidence:
    return Evidence(
        id=key(kind, reference, excerpt)[:24],
        kind=kind,
        reference=reference,
        excerpt=excerpt,
        **metadata,
    )


def invitation_key(application_id: UUID, subject: str, body: str) -> str:
    return key(str(application_id), " ".join(subject.split()), " ".join(body.split()))


def suggest(text: str) -> Details:
    """Extract explicit tokens only. The owner confirms all suggestions."""

    def match(pattern: str) -> str:
        result = re.search(pattern, text, re.I | re.M)
        return result.group(1).strip() if result else ""

    # Skip quoted replies when their boundary is explicit. Ambiguous alternatives remain blank.
    text = re.split(r"(?im)^\s*(?:On .+ wrote:|[- ]*Original Message[- ]*|>)", text, maxsplit=1)[0]
    moved = re.search(r"\b(?:rescheduled|moved)\b[^\n]*?\bto\s+([^\n]+)", text, re.I)
    schedule = moved[1] if moved else text
    dates: set[date] = set()
    for day in re.findall(r"\b\d{4}-\d{2}-\d{2}\b", schedule):
        try:
            dates.add(date.fromisoformat(day))
        except ValueError:
            continue
    months = r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    months += r"Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
    for found in re.finditer(
        rf"\b({months})\s+(\d{{1,2}})(?:st|nd|rd|th)?[,]?\s+(\d{{4}})\b", schedule, re.I
    ):
        try:
            dates.add(datetime.strptime(f"{found[1][:3]} {found[2]} {found[3]}", "%b %d %Y").date())
        except ValueError:
            continue
    clocks = set()
    for clock in re.finditer(
        r"(?<![+\-0-9])\b([01]?\d|2[0-3])(?::([0-5]\d)\s*(AM|PM)?|\s*(AM|PM)\b)",
        schedule,
        re.I,
    ):
        hour = int(clock[1])
        half = clock[3] or clock[4]
        if half:
            if not 1 <= hour <= 12:
                continue
            hour = hour % 12 + (12 if half.upper() == "PM" else 0)
        clocks.add(f"{hour:02}:{clock[2] or '00'}")
    zones = set(
        re.findall(
            r"\b(?:UTC(?:[+-]\d{1,2}(?::\d{2})?)?|GMT|[EPCML][SD]T|IST|"
            r"(?:America|Europe|Asia|Africa|Australia|Pacific)/[A-Za-z_]+(?:/[A-Za-z_]+)?)\b",
            schedule,
        )
    )
    round_name = match(
        r"\b(round\s+[0-9]+|phone screen|technical screen|final (?:round|interview)|assessment)\b"
    )
    topics = match(r"^\s*(?:topics?|focus|covers)\s*:\s*([^\n]+)")
    duration = match(r"\b(\d{1,3})[- ]*(?:minutes?|mins?)\b")
    return Details(
        round=round_name,
        date=next(iter(dates)) if len(dates) == 1 else None,
        time=next(iter(clocks)) if len(clocks) == 1 else None,
        timezone=next(iter(zones)) if len(zones) == 1 else "",
        format=match(r"\b(phone screen|video call|video interview|onsite|in.person|Zoom|Teams)\b"),
        duration_minutes=int(duration) if duration and int(duration) else None,
        topics=[t.strip()[:120] for t in re.split(r"[,;]", topics) if t.strip()][:10],
    )


def requirements(description: str) -> list[Requirement]:
    parsed = extract(description)
    grouped: dict[str, Requirement] = {}
    for item in parsed.skills:
        req = grouped.setdefault(
            item.quote, Requirement(id=key(item.quote)[:24], topic=item.label, wording=item.quote)
        )
        req.skills.append(item.skill)
        if req.topic != item.label:
            req.topic += f" / {item.label}"
    if parsed.education and parsed.education.quote not in grouped:
        wording = parsed.education.quote
        grouped[wording] = Requirement(id=key(wording)[:24], wording=wording, topic="Education")

    def without_bullet(text: str) -> str:
        return text.strip().strip("-–—*•. ").casefold()

    represented = [without_bullet(item) for item in grouped]
    heading = None
    for raw in description.splitlines():
        wording = raw.strip().lstrip("-*• ")
        if not wording:
            continue
        kind = heading_kind(wording)
        if kind is not None:
            heading = kind
        elif not any(quote in without_bullet(wording) for quote in represented) and (
            heading in (Demand.MANDATORY, Demand.PREFERRED)
            or re.match(r"must |experience (?:with|in)|\d+\+? years|ability to ", wording, re.I)
        ):
            wording = wording[:600]
            grouped[wording] = Requirement(id=key(wording)[:24], wording=wording, topic=wording)
    return list(grouped.values())


def _terms(text: str) -> set[str]:
    return set(re.findall(r"[a-z][a-z-]{2,}", text.casefold())) - {
        "experience",
        "required",
        "preferred",
        "ability",
        "with",
        "and",
        "the",
        "explain",
        "working",
        "using",
        "strong",
        "write",
        "develop",
        "knowledge",
    }


def coverage_for(requirement: Requirement, sources: list[Evidence]) -> Coverage:
    required = set(requirement.skills)
    matches = []
    terms = _terms(requirement.wording)
    for source in sources:
        if source.superseded or source.kind not in CANDIDATE_KINDS:
            continue
        overlap = required & {s.key for s in find_skills(source.excerpt)}
        if overlap:
            matches.append((len(overlap), source))
        elif not required and len(terms & _terms(source.excerpt)) >= 2:
            matches.append((0, source))
    matches.sort(
        key=lambda pair: (
            -pair[0],
            -len(terms & _terms(pair[1].excerpt)),
            pair[1].kind == "project",
            pair[1].kind == "coursework",
            pair[1].id,
        )
    )
    best = matches[0][1] if matches else None
    production = bool(PRODUCTION.search(requirement.wording))
    # A technology mention alone cannot establish scope, years, ownership or production use.
    scoped_experience = bool(
        best
        and best.kind in {"resume", "internship"}
        and re.search(
            r"\b(?:built|implemented|developed|tested|used|wrote|maintained|created|operated|deployed)\b",
            best.excerpt,
            re.I,
        )
        and (not production or PRODUCTION.search(best.excerpt))
    )
    demonstrated = bool(
        required
        and matches
        and matches[0][0] == len(required)
        and not re.search(r"\d+\+?\s*years|\b(?:lead|manage|design)\b", requirement.wording, re.I)
        and scoped_experience
    )
    if demonstrated:
        action = "Practice explaining your contribution and how you verified the result."
    elif best:
        action = (
            "Identify what the excerpt demonstrates and name the parts you cannot substantiate."
        )
        if production:
            action = (
                "Find a verified production example or explain the limits of the project evidence."
            )
    else:
        action = (
            "Find a cited example or practice a small exercise and disclose the experience gap."
        )
    return Coverage(
        requirement=requirement,
        status="demonstrated" if demonstrated else "partial" if best else "missing",
        evidence_ids=[source.id for _, source in matches[:3]],
        next_action=action,
    )


def build_plan(inputs: Snapshot, sources: list[Evidence]) -> Plan:
    coverage = [coverage_for(req, sources) for req in requirements(inputs.posting_description)]
    confirmed = next((s.id for s in sources if s.kind == "recruiter-confirmed"), None)
    topics = [
        TopicPlan(
            topic=t, why="Confirmed interview topic.", evidence_ids=[confirmed] if confirmed else []
        )
        for t in inputs.details.topics[:3]
    ]
    posting = {s.excerpt: s.id for s in sources if s.kind == "posting"}
    for row in coverage:
        req = row.requirement
        if len(topics) < 3 and not any(t.topic.casefold() in req.topic.casefold() for t in topics):
            topics.append(
                TopicPlan(
                    topic=req.topic,
                    why="The posting names this requirement.",
                    evidence_ids=[posting[req.wording]] if req.wording in posting else [],
                )
            )
    guidance = [
        s
        for s in sources
        if s.kind in {"official employer guidance", "firsthand report"} and not s.caveat
    ]
    topics = [
        topic.model_copy(
            update={
                "evidence_ids": [
                    *topic.evidence_ids,
                    *[
                        item.id
                        for item in guidance
                        if len(topic.topic) >= 3
                        and topic.topic.casefold() in item.excerpt.casefold()
                    ][:2],
                ],
            }
        )
        for topic in topics
    ]
    for fallback in (
        "Explain a relevant project",
        "Validate a solution with edge cases",
        "Explain a technical trade-off",
    ):
        if len(topics) >= 3:
            break
        topics.append(TopicPlan(topic=fallback, why=GENERAL_PRACTICE, evidence_ids=[]))
    minutes = min(inputs.prep_minutes, 60)
    focus = next(
        (prompt for pattern, prompt in ROLE_FOCUS if re.search(pattern, inputs.role, re.I)),
        "Describe a relevant piece of work and how you checked it.",
    )
    primary = topics[0].topic
    questions = [f"How would you approach a problem involving {primary}?", focus]
    later = []
    if inputs.prep_minutes > minutes:
        later.append(
            PracticeBlock(
                minutes=inputs.prep_minutes - minutes,
                exercise="Repeat a timed mock and work through uncovered requirements.",
            )
        )
    caveats = [s.caveat for s in sources if s.caveat and s.kind not in CANDIDATE_KINDS]
    if not any(
        s.kind in {"official employer guidance", "firsthand report"} and not s.caveat
        for s in sources
    ):
        caveats.append("Company-specific interview guidance is unconfirmed.")
    rehearsal = "Rehearse one cited experience aloud."
    if any(source.kind == "round notes" for source in sources):
        rehearsal = (
            "Rehearse a cited example, then revisit one question recorded in an earlier round."
        )
    return Plan(
        topics=topics,
        next_hour=[
            PracticeBlock(minutes=minutes // 2, exercise=f"Work a timed example on {primary}."),
            PracticeBlock(minutes=minutes // 3, exercise=rehearsal),
            PracticeBlock(
                minutes=minutes - minutes // 2 - minutes // 3,
                exercise="Answer a mock question and review the rubric.",
            ),
        ],
        later=later,
        coverage=coverage,
        questions=questions,
        followups=["What trade-off did you make, and how would you test your decision?"],
        ask=["What does success look like for this role in the first three months?"],
        caveats=list(dict.fromkeys(caveats)),
    )
