"""Tie a reply to an application when it carries no alias of ours.

`alias.py` is the reliable path: an application applied with
`owner+app{id}@…`, the employer replied to it, and the tag is an exact key.
Everything routed that way is certain.

This is for the mail that path never sees, and that set is not small:

- Applications parked as `manual_completion_required` under §2.5, which the
  owner finished by hand in their own browser, using their plain address.
- Aggregator leads we deliberately keep as leads because resolution could not
  find a supported form — also applied by hand.
- Anything applied to before Jobrunner existed, or outside it entirely.

Without this, those replies land in the mailbox and the pipeline board never
learns they happened, which is the one thing a tracker exists to prevent.

## The rule that makes guessing acceptable

An inferred link **attaches the message and stops there**. It never sets
`Application.outcome` and never moves `status`.

That asymmetry is the whole design. Attaching a recruiter's email to the
wrong application is untidy and visible — the owner reads it and sees it does
not belong. Recording a *rejection* on the wrong application is silent and
wrong: a live application shows as dead, the owner stops chasing it, and
nothing ever contradicts the record. §2.4 already says an unanswerable
question parks rather than guesses; this is the same instinct applied to a
different guess.

So the confidence score below decides whether to attach, never whether to
conclude. Only an alias — an exact key — is allowed to change an outcome.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Application, Company, Posting

log = structlog.get_logger(__name__)

#: Below this an inferred link is not worth making. Tuned so a single weak
#: signal never suffices — a company name in a subject line is common.
MIN_CONFIDENCE = 0.6

#: The best candidate must beat the runner-up by this much. Two applications
#: to the same company are the normal case, not the exception, and picking
#: arbitrarily between them is worse than not linking.
MIN_MARGIN = 0.15

#: Mail hosts that say nothing about the employer. A reply sent from Gmail
#: matches every company equally, which is to say none of them.
GENERIC_MAIL_HOSTS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "yahoo.com",
        "icloud.com",
        "proton.me",
        "protonmail.com",
    }
)

#: What ATS notification mail comes from. Being sent via Greenhouse tells us
#: nothing about *which* company, so these are treated like generic hosts.
ATS_MAIL_HOSTS = frozenset(
    {
        "greenhouse.io",
        "us.greenhouse-mail.io",
        "hire.lever.co",
        "ashbyhq.com",
        "myworkday.com",
    }
)

_ADDR_RE = re.compile(r"[\w.+-]+@([\w.-]+)")


@dataclass
class Candidate:
    application: Application
    score: float = 0.0
    signals: list[str] = field(default_factory=list)


@dataclass
class InferredLink:
    application_id: str
    confidence: float
    signals: list[str]
    runner_up: float = 0.0


def sender_domain(from_addr: str) -> str | None:
    """The domain an address is from, lowercased."""
    match = _ADDR_RE.search(from_addr or "")
    return match.group(1).lower() if match else None


def is_generic(domain: str | None) -> bool:
    """Whether a domain identifies no particular employer."""
    if not domain:
        return True
    if domain in GENERIC_MAIL_HOSTS:
        return True
    return any(domain == host or domain.endswith(f".{host}") for host in ATS_MAIL_HOSTS)


def _normalize(name: str) -> str:
    """Company name to a comparable form: 'Acme, Inc.' -> 'acme'."""
    cleaned = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    words = [
        word
        for word in cleaned.split()
        if word not in {"inc", "llc", "ltd", "limited", "corp", "corporation", "co", "the"}
    ]
    return " ".join(words)


def _domain_root(domain: str) -> str:
    """'jobs.acme.co.uk' -> 'acme'. Good enough to compare against a name."""
    parts = [part for part in domain.split(".") if part not in {"www", "jobs", "careers", "mail"}]
    return parts[0] if parts else domain


async def infer(
    session: AsyncSession,
    *,
    candidate_id: object,
    from_addr: str,
    subject: str = "",
    body: str = "",
) -> InferredLink | None:
    """Best guess at which application a message belongs to, or None.

    Returns None whenever the evidence is weak *or* ambiguous. Both are
    failures to identify, and neither is improved by picking something.
    """
    applications = list(
        (
            await session.scalars(
                select(Application).where(Application.candidate_id == candidate_id)
            )
        ).all()
    )
    if not applications:
        return None

    domain = sender_domain(from_addr)
    generic = is_generic(domain)
    haystack = f"{subject}\n{body}".lower()

    scored: list[Candidate] = []
    for application in applications:
        entry = Candidate(application=application)

        posting = (
            await session.get(Posting, application.posting_id) if application.posting_id else None
        )
        company = (
            await session.get(Company, posting.company_id)
            if posting is not None and posting.company_id
            else None
        )

        if company is not None and domain and not generic:
            if company.domain and domain.endswith(company.domain.lower()):
                entry.score += 0.7
                entry.signals.append("sender domain matches the company")
            elif _domain_root(domain) and _domain_root(domain) in _normalize(company.name):
                entry.score += 0.5
                entry.signals.append("sender domain resembles the company name")

        if company is not None:
            name = _normalize(company.name)
            if name and len(name) > 2 and name in haystack:
                entry.score += 0.3
                entry.signals.append("company named in the message")

        if posting is not None and posting.title:
            title = posting.title.lower().strip()
            if len(title) > 6 and title in haystack:
                entry.score += 0.4
                entry.signals.append("posting title quoted in the message")

        if application.url and domain and not generic and domain in application.url.lower():
            entry.score += 0.3
            entry.signals.append("sender domain appears in the application URL")

        if entry.score:
            scored.append(entry)

    if not scored:
        return None

    scored.sort(key=lambda entry: entry.score, reverse=True)
    best = scored[0]
    runner_up = scored[1].score if len(scored) > 1 else 0.0

    if best.score < MIN_CONFIDENCE:
        log.debug("inbound_inference_too_weak", best=round(best.score, 2))
        return None

    if best.score - runner_up < MIN_MARGIN:
        # Two applications to one company is normal. Choosing between them on
        # a hair of difference is how a reply lands on the wrong one.
        log.info(
            "inbound_inference_ambiguous",
            best=round(best.score, 2),
            runner_up=round(runner_up, 2),
        )
        return None

    return InferredLink(
        application_id=str(best.application.id),
        confidence=round(min(best.score, 1.0), 3),
        signals=best.signals,
        runner_up=round(runner_up, 3),
    )
