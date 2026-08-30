"""A résumé critique a person can act on, built from the scores rather than prose.

The shape is borrowed from a widely-shared review prompt: why this might be
rejected, what is strongest, which exact lines need work, and what is missing
that the owner should consider adding. It is a good shape — it asks for lines
rather than adjectives, and it says "do not invent it" about the gaps.

What it does not have, because a chat prompt cannot, is a number behind any of
it. One model runs all three of its stages in a single pass, so the
hiring-manager stage reads its own ATS output and agrees with itself — the
self-grading §45 forbids. And with no before/after, a rewrite that merely made
the model more confident is indistinguishable from one that helped.

So this fills the same shape from `ats.score` and `recruiter.score`, which are
deterministic, independently computed, and blind to whether they are looking at
an original or a rewrite. Every claim below cites the finding that produced it.

## What is deliberately absent

The prompt's fourth output — *rewritten versions of the weak lines* — is not
here. That is the tailorer's job and it already exists, behind the fabrication
guard, which is the only thing standing between a rewrite and an invented
credential. Generating rewrites in a report that bypasses that check would put
unvetted lines in front of the owner with nothing having verified them.

## No fixed counts

The prompt asks for exactly five of each. A résumé with two real problems then
gets three invented ones, which is the same failure as a model padding a list.
Everything here reports what it found.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from packages.tailor import ats, recruiter
from packages.tailor.bullets import LineKind, classify
from packages.tailor.dates import contains_date
from packages.tailor.parse import ParsedResume

__all__ = ["Report", "build", "render"]

#: A finding costing at least this much is a reason someone stops reading.
#: Below it the finding is worth fixing and is not why the résumé was rejected.
_REJECTION_COST = 0.10


def _worth_naming(term: str, requirements: str = "") -> bool:
    """Whether a posting term is worth putting in front of a person.

    **Presentation only. This never touches a score.** `ats.keywords` counts
    the same vocabulary it always did, so no number moves — noise in that list
    lands in the numerator and the denominator alike and mostly cancels.

    Shown to a human it does not cancel at all. The first real run of this
    report told the owner to consider adding `highly`, `mindset`, `eagerness`
    and `using`. A section headed "consider adding" listing filler is worse
    than unhelpful: it reads as advice to stuff the résumé with the posting's
    words, which is the exact behaviour the recruiter score exists to catch.

    `guard.extract_entities` decides, rather than a technology list that would
    drift and would never recognise the skill it has not heard of. It already
    knows `Python`, `Mathematics` and `Kubernetes` are named things and that
    `highly` and `mindset` are not. Multi-word terms are kept whatever they
    look like — `data structures`, `storage systems` — because a phrase in a
    requirements section is a named skill even when neither word is.

    That classifier still passes a capitalized word at the start of a line, so
    `Demonstrated proficiency with Terraform` offered `Demonstrated` as a skill
    to add. The second test is what separates them and needs no vocabulary at
    all: **a real proper noun appears capitalized mid-sentence too.**
    `Terraform` and `Mathematics` occur inside sentences; `Demonstrated` only
    ever opens one. A term the posting never capitalizes except at a boundary
    is a word, not a name.
    """
    from packages.tailor.guard import extract_entities

    cleaned = term.strip()
    if " " in cleaned:
        return True
    if not extract_entities(cleaned):
        return False
    if not cleaned[:1].isalpha() or not cleaned.isalpha():
        # `C++`, `TypeScript/JavaScript`, `.NET` — punctuation is a name's own.
        return True
    if not requirements:
        return True
    return _appears_mid_sentence(cleaned, requirements)


def _appears_mid_sentence(term: str, text: str) -> bool:
    """Whether `term` is ever capitalized somewhere that is not a boundary.

    A boundary is the start of a line or the end of a previous sentence. A
    word capitalized only there was capitalized by grammar; one capitalized
    inside a sentence was capitalized because it is a name.
    """
    for match in re.finditer(rf"\b{re.escape(term)}\b", text):
        head = text[: match.start()]
        line = head[head.rfind("\n") + 1 :]
        if not line.strip():
            continue  # first thing on its line
        if line.rstrip().endswith((".", "!", "?", ":", ";")):
            continue  # first word of a sentence
        return True
    return False


@dataclass
class Report:
    """The five-part review, with the numbers that produced each part."""

    ats_parse: float
    ats_keywords: float
    recruiter_overall: float
    shortlist: str
    #: Why a reader might stop. Ordered by what it costs, not by how it reads.
    rejection_risks: list[str] = field(default_factory=list)
    #: What is working, and the evidence for saying so.
    strengths: list[str] = field(default_factory=list)
    #: Verbatim lines with the objection to each.
    lines_to_fix: list[tuple[str, str]] = field(default_factory=list)
    #: Terms the posting asks for that the résumé does not back. Not a to-do
    #: list — writing one in unsupported would be fabrication under §2.1.
    consider_adding: list[str] = field(default_factory=list)


def build(resume: ParsedResume, job_description: str) -> Report:
    """Score the résumé and turn the findings into something actionable."""
    machine = ats.score(resume, job_description)
    person = recruiter.score(resume, job_description)
    # The same view of the posting the scorer used, so "does this word ever
    # appear mid-sentence" is asked of the requirements rather than the prose.
    requirements = ats._requirements_text(job_description)

    risks: list[str] = []
    fixes: list[tuple[str, str]] = []
    for finding in sorted([*machine.findings, *person.findings], key=lambda f: -f.cost):
        if finding.line:
            fixes.append((finding.line.strip(), finding.detail))
        elif finding.cost >= _REJECTION_COST:
            risks.append(finding.detail)

    strengths: list[str] = []
    bullets = [
        line
        for section in ("experience", "projects")
        for line in resume.section(section)
        if line.strip() and classify(line) is LineKind.BULLET
    ]
    quantified = [line for line in bullets if recruiter._QUANTIFIED.search(line)]
    if quantified:
        strengths.append(
            f"{len(quantified)} of {len(bullets)} bullets carry a number, so the claims read "
            "as results rather than responsibilities"
        )
    backed = [term for term in machine.supported if _worth_naming(term, requirements)]
    if backed:
        strengths.append(
            "the posting's own terms already backed by the résumé: " + ", ".join(backed[:10])
        )
    if person.credibility >= 0.9:
        strengths.append(
            "every listed skill is evidenced somewhere in the experience — nothing "
            "claimed that the document cannot show"
        )
    if machine.parse >= 0.8:
        strengths.append(f"an ATS can segment this document cleanly ({machine.parse:.0%})")
    if any(contains_date(line) for line in resume.section("experience")):
        strengths.append("the experience is dated, so a parser can place it in time")

    return Report(
        ats_parse=machine.parse,
        ats_keywords=machine.keywords,
        recruiter_overall=person.overall,
        shortlist=person.shortlist,
        rejection_risks=risks,
        strengths=strengths,
        lines_to_fix=fixes,
        consider_adding=[term for term in machine.missing if _worth_naming(term, requirements)],
    )


def render(report: Report) -> str:
    """The report as text. Every section says what produced it."""
    out: list[str] = []
    out.append("SCORES")
    out.append(f"  ATS parse        {report.ats_parse:.0%}   can a machine read it")
    out.append(f"  ATS keywords     {report.ats_keywords:.0%}   does it back what the posting asks")
    out.append(f"  Recruiter        {report.recruiter_overall:.0%}   would a person shortlist it")
    out.append(f"  Verdict          {report.shortlist}")

    def section(title: str, rows: list[str], empty: str) -> None:
        out.append("")
        out.append(title)
        if not rows:
            out.append(f"  {empty}")
            return
        for row in rows:
            out.append(f"  - {row}")

    section(
        "WHY THIS MIGHT BE REJECTED",
        report.rejection_risks,
        "nothing scored above the threshold that makes a reader stop",
    )
    section("WHAT IS STRONGEST", report.strengths, "no strength signal fired")

    out.append("")
    out.append("EXACT LINES TO IMPROVE")
    if not report.lines_to_fix:
        out.append("  no individual line was flagged")
    for line, why in report.lines_to_fix:
        out.append(f"  - {line[:96]}")
        out.append(f"      {why}")

    section(
        "CONSIDER ADDING — only if true, never invented",
        report.consider_adding[:15],
        "the résumé backs everything this posting names",
    )
    out.append("")
    out.append("Rewrites are not offered here. That is the tailorer's job and it runs behind")
    out.append("the fabrication guard; an unvetted rewrite in a report is a fabricated line")
    out.append("with nothing having checked it.")
    return "\n".join(out)
