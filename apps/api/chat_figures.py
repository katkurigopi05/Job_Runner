"""Rates and charts of the owner's applications, computed in code.

The assistant's model may repeat a number its context states and may derive
none (`assistant.system`): a small local model doing arithmetic writes numbers
that look right. So the two things the dock could not do are both done here,
from the database:

- **A rate question** ("what is my interview rate?") gets a `RATES` section:
  each rate with its counts, from `packages/analytics/funnel.py`, which also
  decides when a band holds too few applications to read a rate from. The
  model repeats them.
- **A chart request** ("chart my applications by status") is answered here
  before any model is chosen, like the crawl command, with the chart's data
  on the reply for the dock to draw. Every number in it is a count.

A share of *postings* ("what percentage of postings are AI roles") is
`postings_share`, from retrieval (PR #221). This module is about the owner's
own applications, and asks for one of their words before it answers.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.analytics import funnel
from packages.core.enums import ApplicationStatus
from packages.core.models import Application
from packages.core.schemas import ChatChart, ChatChartPoint, ChatReply

#: Weeks a weekly chart covers, ending with the current one.
WEEKS = 12

_CHART_WORD = r"(?:(?:bar|line|pie)\s+)?(?:chart|graph|plot|histogram|diagram)s?|visuali[sz]e"
#: A chart word that opens the request ("plot …", "show me a bar chart …"),
#: or one beside "my". A bare "graph" is also a job subject: "Any jobs with
#: graph neural networks?" is a search, not a request to draw.
_ASKS_FOR_CHART = re.compile(
    r"^\s*(?:(?:can|could|would|will)\s+you\s+|please\s+"
    r"|(?:show|give|make|draw|build|create)\s+(?:me\s+)?(?:an?\s+|the\s+)?)*"
    rf"(?:{_CHART_WORD})\b"
    rf"|\b(?:{_CHART_WORD})\b.*\bmy\b|\bmy\b.*\b(?:{_CHART_WORD})\b",
    re.IGNORECASE,
)


class ChartSubject(StrEnum):
    STATUS = "status"
    WEEKLY = "weekly"
    SCORE = "score"


_SCORE_WORDS = re.compile(r"\b(?:scores?|scoring)\b", re.IGNORECASE)
_TIME_WORDS = re.compile(
    r"\b(?:weeks?|weekly|over\s+time|time|trend|timeline|per\s+day|daily|months?|monthly)\b",
    re.IGNORECASE,
)
_APPLICATION_WORDS = re.compile(
    r"\b(?:applications?|applied|submitted|submissions?|repl(?:y|ies)|responses?|responded"
    r"|interviews?|callbacks?|rejections?|rejected|offers?|hear(?:d)?\s+back|pipeline"
    r"|status(?:es)?|stages?|funnel)\b",
    re.IGNORECASE,
)
#: Subjects this module has no chart of. Charting them from application data
#: would draw the wrong thing under the right title.
_OTHER_SUBJECTS = re.compile(
    r"\b(?:postings?|jobs?|roles?|compan(?:y|ies)|skills?|openings?|listings?|salar(?:y|ies))\b",
    re.IGNORECASE,
)

_RATE_WORDS = re.compile(
    r"\b(?:rates?|ratios?|percent(?:age)?s?|proportion|fraction|how\s+often|conversion"
    r"|share\s+of)\b|%",
    re.IGNORECASE,
)

#: What `ChartSubject` each chart is drawn about, for the reply that lists them.
CHARTABLE = (
    "your applications by status",
    "your applications per week",
    "reply rate by match score",
)


def asks_for_chart(question: str) -> bool:
    return bool(_ASKS_FOR_CHART.search(question))


def chart_subject(question: str) -> ChartSubject | None:
    """Which chart was asked for, or None for one this module cannot draw."""
    about_applications = bool(_APPLICATION_WORDS.search(question))
    if _OTHER_SUBJECTS.search(question) and not about_applications:
        return None
    if _SCORE_WORDS.search(question):
        return ChartSubject.SCORE
    if _TIME_WORDS.search(question):
        return ChartSubject.WEEKLY
    if about_applications or re.search(r"\bmy\b", question, re.IGNORECASE):
        return ChartSubject.STATUS
    return None


def asks_for_application_rates(question: str) -> bool:
    """A rate word and an application word: "what share of my applications got replies"."""
    return bool(_RATE_WORDS.search(question)) and bool(_APPLICATION_WORDS.search(question))


def _percent(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%"


def _status_order(status: str) -> int:
    order = [s.value for s in ApplicationStatus]
    return order.index(status) if status in order else len(order)


async def _status_counts(session: AsyncSession) -> dict[str, int]:
    rows = (
        await session.execute(select(Application.status, func.count()).group_by(Application.status))
    ).all()
    counts = {status: count for status, count in rows if count}
    return dict(sorted(counts.items(), key=lambda item: (_status_order(item[0]), item[0])))


async def rates_section(session: AsyncSession) -> str:
    """Every rate a question about the owner's applications can want, with its counts."""
    counts = await _status_counts(session)
    total = sum(counts.values())
    header = (
        "RATES (computed from the database; repeat a percentage only as written here, "
        "with its counts, and compute none of your own):"
    )
    if not total:
        return f"{header}\n  no applications yet, so there is no rate of any kind"

    report = await funnel.build(session)
    stages = report.stages
    lines = [
        header,
        f"  applications: {total} in all",
        "  share by status: "
        + ", ".join(
            f"{status} {count} of {total} ({_percent(count, total)})"
            for status, count in counts.items()
        ),
    ]
    submitted = stages.submitted
    if not submitted:
        lines.append("  none submitted yet, so there is no reply rate")
        return "\n".join(lines)

    readable = submitted >= 5
    too_few = f" — only {submitted} submitted, too few to read a rate from"

    def rate(label: str, part: int) -> str:
        shown = f" ({_percent(part, submitted)})" if readable else too_few
        return f"  {label}: {part} of {submitted} submitted{shown}"

    lines += [
        rate("any reply from an employer", stages.answered),
        rate("a person engaged (interview, offer, request, assessment)", stages.engaged),
        "  by the match score that queued them, a person engaged:",
    ]
    for bucket in reversed(report.buckets):
        if not bucket.submitted:
            lines.append(f"    {bucket.label}: none submitted")
        elif bucket.is_meaningful:
            lines.append(
                f"    {bucket.label}: {bucket.engaged} of {bucket.submitted} submitted engaged "
                f"({_percent(bucket.engaged, bucket.submitted)})"
            )
        else:
            lines.append(
                f"    {bucket.label}: {bucket.engaged} of {bucket.submitted} submitted engaged"
                " — too few to read a rate"
            )
    if report.unscored:
        lines.append(
            f"  applications with no match score, not in the bands above: {report.unscored}"
        )
    return "\n".join(lines)


def _reply(text: str, chart: ChatChart | None = None) -> ChatReply:
    return ChatReply(
        reply=text, provider="chart", grounded=True, local=True, shared_mail=False, chart=chart
    )


async def _status_chart(session: AsyncSession, counts: dict[str, int]) -> ChatReply:
    total = sum(counts.values())
    chart = ChatChart(
        title="Applications by status",
        unit="applications",
        caption=f"All {total} applications, every company, by where each stands now.",
        points=[ChatChartPoint(label=status, value=count) for status, count in counts.items()],
    )
    parts = ", ".join(f"{status} {count}" for status, count in counts.items())
    return _reply(f"{total} applications in all: {parts}.", chart)


def _week_start(moment: datetime) -> datetime:
    day = moment.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return day - timedelta(days=day.weekday())


async def _weekly_chart(session: AsyncSession) -> ChatReply:
    this_week = _week_start(datetime.now(UTC))
    weeks = [this_week - timedelta(weeks=n) for n in range(WEEKS - 1, -1, -1)]
    created = (await session.scalars(select(Application.created_at))).all()
    in_range = Counter(_week_start(at) for at in created if at >= weeks[0])
    older = sum(1 for at in created if at < weeks[0])
    points = [
        ChatChartPoint(label=week.strftime("%b %d"), value=in_range.get(week, 0)) for week in weeks
    ]
    shown = sum(in_range.values())
    caption = (
        f"Applications created each week, the last {WEEKS} weeks; a week starts on Monday (UTC)."
        + (f" {older} older {'one is' if older == 1 else 'ones are'} not shown." if older else "")
    )
    chart = ChatChart(
        title="Applications per week", unit="applications", caption=caption, points=points
    )
    busiest = max(points, key=lambda point: point.value)
    text = f"{shown} applications in the last {WEEKS} weeks." + (
        f" The busiest was the week of {busiest.label}, with {int(busiest.value)}." if shown else ""
    )
    return _reply(text, chart)


async def _score_chart(session: AsyncSession) -> ChatReply:
    report = await funnel.build(session)
    points = []
    said = []
    for bucket in report.buckets:
        rate = bucket.engagement_rate
        value = round(100 * rate, 1) if rate is not None else 0.0
        note = (
            f"{bucket.engaged} of {bucket.submitted} submitted"
            if bucket.submitted
            else "none submitted"
        )
        points.append(
            ChatChartPoint(
                label=bucket.label, value=value, note=note, reliable=bucket.is_meaningful
            )
        )
        if bucket.submitted:
            said.append(
                f"{bucket.label}: {note}"
                + (f" ({value:g}%)" if bucket.is_meaningful else ", too few to read")
            )
    caption = (
        "Share of submitted applications where a person engaged (interview, offer, request, "
        "assessment), by the match score that queued them. A band with fewer than 5 submitted "
        "is too few to read a rate from."
        + (f" {report.unscored} with no match score are not shown." if report.unscored else "")
    )
    chart = ChatChart(title="Reply rate by match score", unit="%", caption=caption, points=points)
    text = (
        "A person engaged, by match score: " + "; ".join(said) + "."
        if said
        else "Nothing has been submitted yet, so there is no reply rate by score."
    )
    return _reply(text, chart if said else None)


async def chart_reply(session: AsyncSession, question: str) -> ChatReply:
    """The chart asked for, drawn from the database, or what can be drawn instead."""
    subject = chart_subject(question)
    if subject is None:
        listed = ", ".join(CHARTABLE[:-1]) + f", and {CHARTABLE[-1]}"
        return _reply(
            f"I can chart three things from your own data: {listed}. Ask for one of those, "
            'for example "chart my applications by status".'
        )
    counts = await _status_counts(session)
    if not counts:
        return _reply("No applications yet, so there is nothing to chart.")
    if subject is ChartSubject.WEEKLY:
        return await _weekly_chart(session)
    if subject is ChartSubject.SCORE:
        return await _score_chart(session)
    return await _status_chart(session, counts)
