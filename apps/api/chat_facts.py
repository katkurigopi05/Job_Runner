"""Render factual search answers from typed counts, never model arithmetic.

The renderer knows which predicate each count measures. It does not infer
remote eligibility from mentions or an exhaustive total from supplied examples.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from packages.core.notify import ParkReason, asks
from packages.core.schemas import ChatCountFact
from packages.matching.retrieve import Retrieval

_COUNT_QUESTION = re.compile(
    r"\b(?:how\s+many|what\s+(?:percent(?:age)?|share|fraction)|"
    r"(?:percent(?:age)?|share|fraction)\s+of|"
    r"count\s+(?:the\s+)?(?:jobs|postings|roles)|"
    r"total\s+(?:number|jobs|postings|roles))\b",
    re.I,
)


def one_line(value: object) -> str:
    return " ".join(str(value or "").split())


def asks_for_counts(question: str) -> bool:
    return bool(_COUNT_QUESTION.search(question))


def asks_for_application_snapshot(question: str) -> bool:
    """Only questions the status snapshot can answer without review or mail detail.

    The caller still uses the shared first-person application routing rule.
    A selected review record can contain unanswered questions beyond this snapshot.
    """
    words = one_line(question).casefold().rstrip("?.!")
    return words == "what needs me" or bool(
        re.fullmatch(r"how many applications do i have", words)
        or (
            re.search(r"\bstatus(?:es)?\b", words)
            and not re.search(r"\b(?:why|questions?|reply|replied|mail|reason)\b", words)
        )
    )


@dataclass(frozen=True)
class ApplicationFact:
    title: str
    url: str
    status: str
    failure_reason: str | None
    created: str
    updated: str

    def line(self) -> str:
        return (
            f"{one_line(self.title)}; url={one_line(self.url)}; status={one_line(self.status)}; "
            f"failure_reason={one_line(self.failure_reason) or 'none'}; "
            f"created={one_line(self.created)}; updated={one_line(self.updated)}"
        )


@dataclass(frozen=True)
class CompanyApplications:
    name: str
    applications: tuple[ApplicationFact, ...]

    def context(self) -> str:
        lines = [
            f"YOUR APPLICATIONS AT {one_line(self.name)}:",
            f"  {len(self.applications)} application"
            f"{'' if len(self.applications) == 1 else 's'} at {one_line(self.name)}; "
            "global status counts cover all companies.",
        ]
        if not self.applications:
            lines.append(f"  no applications at {one_line(self.name)}")
        lines.extend(f"  - {application.line()}" for application in self.applications)
        return "\n".join(lines)

    def answer(self) -> str:
        if not self.applications:
            return f"No applications at {one_line(self.name)}."
        return (
            f"{len(self.applications)} application"
            f"{'' if len(self.applications) == 1 else 's'} at {one_line(self.name)}.\n"
            + "\n".join(application.line() for application in self.applications)
        )


@dataclass(frozen=True)
class OwnerFacts:
    context: str
    statuses: tuple[tuple[str, int], ...]
    waiting: tuple[tuple[ParkReason, int], ...]
    application: ApplicationFact | None = None

    def answer(self) -> str:
        if self.application is not None:
            return self.application.line()
        lines = [
            f"{sum(count for _, count in self.statuses)} applications across all companies.",
            "Statuses: "
            + (
                ", ".join(f"{one_line(status)}: {count}" for status, count in self.statuses)
                or "none"
            )
            + ".",
            "Waiting on you (already counted above, not additional applications):",
        ]
        lines.extend(f"{reason.value}: {count} ({asks(reason)})" for reason, count in self.waiting)
        if not self.waiting:
            lines.append("Nothing is waiting on you.")
        return "\n".join(lines)


def count_facts(found: Retrieval) -> tuple[ChatCountFact, ...]:
    """Project existing retrieval values, retaining every count's own meaning."""
    if not found.attempted:
        return ()
    scope = (
        "the Bay Area"
        if found.place_total is not None
        else "your search area"
        if found.area
        else "the searched area"
    )
    facts: list[ChatCountFact] = []

    def add(
        key: str,
        value: int | None,
        predicate: str,
        *,
        unit: Literal["postings", "companies"] = "postings",
        exact: bool = True,
        over: str | None = None,
    ) -> None:
        facts.append(
            ChatCountFact(
                key=key,
                value=value,
                coverage="unknown" if value is None else "exact" if exact else "at_least",
                unit=unit,
                predicate=predicate,
                scope=over or scope,
            )
        )

    add("searched", found.searched, "searched open postings", over="all open postings")
    add("role_total", found.role_total, "requested role titles")
    add("title_total", found.title_total, "requested words in titles")
    add(
        "matched",
        found.match_total,
        "requested term mentions" if found.matched_term else "company postings",
    )
    if found.company_total is not None:
        add("companies", found.company_total, "companies of matched postings", unit="companies")
    if found.beyond_role:
        add("subject_title", found.beyond_in_title, "role titles containing the additional subject")
        add(
            "subject_description",
            found.beyond_mentioned,
            "other role descriptions mentioning an additional subject term",
        )
    if found.place_total is not None:
        add(
            "place_total",
            found.place_total,
            "postings establishing a Bay Area office",
            exact=found.place_counts_complete,
        )
        add(
            "remote_or_unplaced",
            found.remote_or_unplaced_not_listed,
            "remote or unplaced roles omitted by the regional filter",
            exact=found.place_counts_complete,
            over="your standing search area before the place filter",
        )
    if found.required_skill is not None:
        count = found.required_skill
        add(
            "required_skill",
            count.required,
            f"direct requirements for {count.skill}",
            exact=count.complete,
        )
        add(
            "required_companies",
            count.company_total,
            f"companies directly requiring {count.skill}",
            unit="companies",
            exact=count.complete,
        )
    return tuple(facts)


def render_postings(found: Retrieval, evidence: Mapping[uuid.UUID, list[str]] | None = None) -> str:
    """State exact counts and sample limits, followed by supplied source facts."""
    # Validate coverage/values before rendering even callers outside chat.prepare.
    count_facts(found)
    scope = (
        "the Bay Area"
        if found.place_total is not None
        else "your search area"
        if found.area
        else "the searched area"
    )
    lines = [f"Searched {found.searched} of {found.searched + found.unsearchable} open postings."]
    if found.area:
        lines.append(f"Search area: {one_line(found.area)}.")
    elif found.area_waived:
        lines.append("Your standing search-area filter was waived for the named place.")
    if found.required_skill is not None:
        lines.append(found.required_skill.in_words())
    if found.share is not None:
        share = found.share
        percent = (
            f"{share.percentage:.2f}%"
            if share.percentage is not None
            else "undefined because the denominator is zero"
        )
        lines.append(
            f"Computed share: {share.matching} of {share.open_total} open postings "
            f"in {share.scope} ({percent})."
        )
    elif found.share_requested:
        lines.append(
            "Percentage unavailable: a required-skill share has not been computed."
            if found.required_skill is not None
            else "Percentage unavailable: the complete matching count for this compound search "
            "is unknown. The available corpus or role count cannot supply that numerator."
        )
    if found.role_total is not None:
        lines.append(
            f"{found.role_total} open postings with "
            f"{' or '.join(map(one_line, found.roles))} titles in {scope}."
        )
        if found.kinds:
            lines.append(
                "Kinds: "
                + "; ".join(f"{number} {one_line(kind)}" for kind, number in found.kinds)
                + "."
            )
    elif found.title_total is not None:
        lines.append(
            f"{found.title_total} open postings with "
            f"{' and '.join(map(one_line, found.title_words))} in their titles in {scope}."
        )
    elif found.match_total is not None:
        if found.matched_term:
            company_count = (
                f" at {found.company_total} companies" if found.company_total is not None else ""
            )
            lines.append(
                f"{found.match_total} postings{company_count} mention "
                f"{one_line(found.matched_term)} in {scope}. "
                "These are mentions, not requirements or proof of actual use."
            )
        else:
            lines.append(
                f"{found.match_total} open postings at "
                f"{', '.join(map(one_line, found.companies))} in {scope}."
            )
    elif found.sponsorship is None:
        lines.append(
            "The total matching count is unknown; the ranked sample does not "
            "establish an exhaustive total."
        )
    if found.beyond_role and found.role_total is not None:
        subject = ", ".join(map(one_line, found.beyond_role))
        parts = [f"{found.beyond_in_title} titles contain the additional subject ({subject})"]
        said = found.beyond_in_title
        if found.beyond_mentioned is not None:
            said += found.beyond_mentioned
            parts.append(
                f"{found.beyond_mentioned} more descriptions mention an additional subject term"
            )
        else:
            parts.append("description coverage for this subject is unknown")
        parts.append(
            f"{found.role_total - said} other role postings do not establish it "
            "in the counted fields"
        )
        lines.append("; ".join(parts) + ".")
        if any(term.casefold() == "remote" for term in found.beyond_role):
            lines.append("Text mentions do not establish remote eligibility.")
    if found.place_total is not None:
        reach = "" if found.place_counts_complete else " among retrieved candidates"
        lines.append(
            f"Place filter applied — in the Bay Area: {found.place_total}{reach}; "
            f"{found.search_area_total} matching postings{reach} "
            "in your search area before this filter."
        )
        lines.append(
            f"{found.remote_or_unplaced_not_listed} remote or unplaced roles in your "
            "search area are not listed because they do not establish a Bay Area "
            f"office{reach}."
        )
    if found.outside_area:
        lines.append(
            f"{found.outside_area} matching postings outside your search area were left out."
        )
    if found.sponsorship is not None:
        sponsor = found.sponsorship
        lines.append(
            f"Over {one_line(sponsor.over)}: {sponsor.offered} state sponsorship is offered, "
            f"{sponsor.refused} state it is not, and {sponsor.unstated} are unresolved "
            "or silent. Any eligibility conditions still apply."
        )
    if found.level is not None:
        level = found.level
        lines.append(
            f"For level {one_line(level.said)}: {level.at_level} titles name it, "
            f"{level.unknown} name no level and {level.other} name a different level. "
            "Titles naming a different level are not listed."
        )
    if not found.passages:
        lines.append(f"No retrieved postings are available in {scope} for this question.")
    else:
        lines.append(
            f"The closest {len(found.passages)} retrieved postings follow. "
            "These are retrieved examples; matching coverage is stated above."
        )
        if found.more:
            lines.append("Further retrieved sources are listed separately.")
        for passage in found.passages:
            label = f"[{passage.label}] "
            metadata = " — ".join(
                one_line(v) for v in (passage.title, passage.company, passage.location) if v
            )
            lines.append(label + metadata)
            lines.extend(one_line(line) for line in (evidence or {}).get(passage.posting_id, []))
            if (
                found.sponsorship is not None or found.required_skill is not None
            ) and passage.excerpt:
                lines.append("Source: " + one_line(passage.excerpt))
    return "\n".join(lines)
