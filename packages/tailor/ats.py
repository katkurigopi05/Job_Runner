"""Score a résumé the way an applicant tracking system reads it.

Two questions, answered separately, because they fail independently and a
single averaged number hides which one broke:

- **Parse.** Can a machine turn this document into fields? An ATS extracts
  contact details, segments the document by heading, and reads dates out of
  each entry. A résumé it cannot segment is not a low-ranked résumé; it is a
  row of empty columns, and no amount of keyword matching rescues it.
- **Keywords.** Of the vocabulary this posting actually asks for, how much does
  the résumé already back? `packages/tailor/keywords.py` computes exactly that
  split and this reuses it rather than asking the question a second way — two
  implementations of "does the résumé support this term" is how the tailorer
  and the guard end up disagreeing.

## What this is not

It is not a real ATS. Greenhouse, Lever, Workday and Taleo each parse
differently, none publishes its algorithm, and the "ATS score" sold by résumé
sites is a marketing number with no referent. This is a heuristic over defects
that are *observable in the document* — a heading no parser recognizes, a date
fused to the word before it, contact details that are not there. Every finding
names the line it came from, so the number is auditable and the owner can
disagree with it.

## Why it exists at all

`docs/REFERENCE.md` §3.6 warns against tuning the rewriter against the
fabrication guard's own pass rate: the guard is the one referee we control, and
a rewrite can satisfy it while reading worse. CLAUDE.md §7 makes the same point
and asks that any such change be reported beside "a second measure". This is
that second measure. It is deliberately *not* computed from anything the
rewriter optimizes — the parse half never looks at the posting, and the keyword
half counts the posting's vocabulary rather than the guard's verdicts.

The number to watch is the pair, before and after tailoring. A rewrite that
raises keyword coverage while lowering the parse score has made the document
worse in the way that matters most, and averaging the two would hide it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from packages.tailor.guard import SourceCorpus, normalize
from packages.tailor.keywords import analyze
from packages.tailor.parse import ParsedResume, _match_section

#: A bullet this long is one an ATS keeps and a human skips. Not a parse
#: failure, so it costs little — but the owner's own résumé has one of 431
#: characters, and a reader who stops mid-sentence never reaches the metric.
MAX_BULLET_CHARS = 300

#: Below this, a section heading is doing no work. A résumé with an Experience
#: heading and one line under it parsed wrong somewhere upstream.
MIN_SECTION_LINES = 1

#: Month names, whole and abbreviated. Used to catch a date fused to the word
#: before it — the residue of a tab that did not survive extraction.
_MONTHS = (
    "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
    "|january|february|march|april|june|july|august"
    "|september|october|november|december"
)

#: `…AnalyticsJan 2025`, `…EngineeringAug 2020`. A lowercase letter followed
#: immediately by a capitalized month is not a word — it is two fields that
#: were separated by a tab in the source document and are not separated in the
#: extracted text.
#:
#: Scoped this narrowly on purpose. The general "lowercase then uppercase"
#: signal cannot tell `BayHayward` from `HuggingFace`, `WebGPU`, `BigQuery` or
#: `XGBoost`, all of which are correct and all of which appear in the owner's
#: résumé. A month or a four-digit year on the right-hand side has no such
#: false positives, and dates are where the tab is used, because that is what
#: right-aligns them.
_FUSED_DATE_RE = re.compile(rf"[a-z](?:{_MONTHS})\b|[a-z](?:19|20)\d{{2}}\b", re.IGNORECASE)

#: A date range, however it is punctuated. Absence of one under Experience is
#: the single most common reason an ATS files a job with no start date.
_DATE_RANGE_RE = re.compile(
    rf"\b(?:{_MONTHS})\w*\.?\s*\.?\s*(?:19|20)\d{{2}}\b|\b(?:19|20)\d{{2}}\s*[-–—]\s*"
    rf"(?:(?:19|20)\d{{2}}|present|current)\b",
    re.IGNORECASE,
)

#: A heading is short, has no terminal punctuation, and is not a sentence.
#: Deliberately looser than `parse._match_section`, because the whole point is
#: to find the lines that look like headings to a human and to an ATS but not
#: to our parser.
_MAX_HEADING_WORDS = 4

#: Characters that appear in résumé *content* and never in a section heading.
#:
#: This list is doing the real work. Without it the test fired on every project
#: title and every tech-stack line in the owner's résumé — `Python, FastAPI,
#: React, HuggingFace Transformers, Qdrant` is short, capitalized and
#: unpunctuated, and reporting it as a missed heading is both wrong and the
#: kind of wrong that trains an owner to ignore the report.
#:
#: A heading names a section. It carries no list (comma), no link or annotation
#: (bracket, parenthesis), no date or count (digit), and no title/subtitle break
#: (dash). `CERTIFICATIONS & TRAINING` survives all five; `Project Director —
#: AI-Native Photo/Video/Audio Editor   [GitHub]` fails three.
_NOT_IN_A_HEADING = ",[]()0123456789—–•|/"

#: Sections a résumé is expected to have. Projects substitutes for experience —
#: `packages/tailor/bullets.py` records why, and the owner's own résumé is the
#: case: a student with no employment section and their whole record under
#: Projects.
_REQUIRED_EITHER: tuple[tuple[str, ...], ...] = (("experience", "projects"), ("education",))
_EXPECTED: tuple[str, ...] = ("skills",)


#: Where a posting stops describing the job and starts discharging legal duty.
#:
#: Everything from here down is EEO, accommodation and pay-transparency
#: language. It is long, it is repetitive, and frequency ranking loves it: on
#: real Pinterest postings the terms the résumé "failed to match" included
#: `gender`, `qualified`, `consideration`, `applicants` and `employment` —
#: none of which anyone is being asked to have.
#:
#: Matched case-insensitively against the whole text; the earliest hit wins.
_LEGAL_FOOTER_MARKERS: tuple[str, ...] = (
    "equal opportunity employer",
    "equal employment opportunity",
    "without regard to race",
    "regardless of race",
    "all qualified applicants",
    "reasonable accommodation",
    "e-verify",
    "pay transparency",
    "we are an equal",
)


def _requirements_text(job_description: str) -> str:
    """The posting with its legal footer removed.

    Scoped to this module on purpose. `keywords.job_terms` would also read
    better on truncated text — its 40-term budget is currently spent partly on
    EEO vocabulary — but its output is `TermReport.missing`, which `rewrite.vet`
    uses as the terms a rewrite may not borrow. Shortening that list is a change
    to what the guard refuses and deserves its own evidence, not a side effect
    of improving a score.
    """
    lowered = job_description.lower()
    cuts = [lowered.find(marker) for marker in _LEGAL_FOOTER_MARKERS]
    hits = [c for c in cuts if c > 0]
    if not hits:
        return job_description
    cut = min(hits)
    # A posting that is *mostly* footer is one we have misread; keep it whole
    # rather than scoring almost nothing.
    if cut < len(job_description) * 0.3:
        return job_description
    return job_description[:cut]


@dataclass(frozen=True)
class Finding:
    """One observable defect, with the evidence and what it costs."""

    code: str
    detail: str
    #: Deducted from the parse score. See `AtsReport.parse`.
    cost: float
    #: The offending line, verbatim, so the owner can check the call.
    line: str | None = None

    def __str__(self) -> str:
        where = f" — {self.line[:80]!r}" if self.line else ""
        return f"[{self.code}] {self.detail}{where}"


@dataclass(frozen=True)
class AtsReport:
    """How an ATS is likely to read this résumé, and against this posting.

    `parse` and `keywords` answer different questions and are never averaged
    into a single number without both being shown — see the module docstring.
    """

    #: 1.0 means nothing observable stops a parser. Starts at 1.0 and each
    #: finding deducts its cost; floored at 0.
    parse: float
    #: Share of the posting's salient terms the résumé backs. 0.0 with no
    #: posting supplied, in which case only `parse` is meaningful.
    keywords: float
    findings: list[Finding] = field(default_factory=list)
    supported: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    #: False when no posting was supplied, so `keywords` must not be read.
    scored_against_posting: bool = False

    @property
    def overall(self) -> float:
        """A convenience, weighted toward parse. Read the parts instead.

        Parse is weighted higher because it gates: an ATS that cannot find the
        Experience section does not go on to match keywords inside it. The
        weighting is a judgement, not a measurement, which is why the parts are
        the documented answer and this is the convenience.
        """
        if not self.scored_against_posting:
            return round(self.parse, 3)
        return round(self.parse * 0.6 + self.keywords * 0.4, 3)

    def summary(self) -> str:
        parts = [f"parse {self.parse:.0%}"]
        if self.scored_against_posting:
            parts.append(f"keywords {self.keywords:.0%}")
        parts.append(f"{len(self.findings)} finding{'s' if len(self.findings) != 1 else ''}")
        return ", ".join(parts)


def _looks_like_heading(line: str) -> bool:
    """Whether a human would read this line as a section heading."""
    stripped = line.strip()
    if not stripped or len(stripped.split()) > _MAX_HEADING_WORDS:
        return False
    if stripped.endswith((".", ",", ";", ":")):
        return False
    if any(c in _NOT_IN_A_HEADING for c in stripped):
        return False
    letters = [c for c in stripped if c.isalpha()]
    if not letters:
        return False
    # All-caps is how a résumé writes a heading, and it is the only form this
    # accepts. Title Case was tried and matched project titles — the cost of
    # missing a lowercase heading is one unreported finding; the cost of the
    # looser test was ten false ones burying the two that were real.
    return all(c.isupper() for c in letters)


def _unrecognized_headings(resume: ParsedResume) -> list[Finding]:
    """Heading-shaped lines the parser filed as content.

    This is the finding that explains the others. `CERTIFICATIONS & TRAINING`
    normalizes to `certifications training`, which is not in
    `SECTION_PATTERNS`, so every certification below it was filed under
    whatever section preceded it — in the owner's résumé, under Skills, which
    then contained no skills at all. `ACHIEVEMENTS & ACTIVITIES` did the same
    thing to Projects.

    An ATS makes the same mistake for the same reason, and the consequence is
    worse there: the certifications are indexed as skills, or as nothing.
    """
    findings: list[Finding] = []
    for name, lines in resume.sections.items():
        for line in lines:
            if not _looks_like_heading(line):
                continue
            if _match_section(line) is not None:
                continue
            findings.append(
                Finding(
                    code="unrecognized_heading",
                    detail=(
                        f"looks like a section heading but was filed as content under "
                        f"{name!r}; everything below it inherits the wrong section"
                    ),
                    cost=0.12,
                    line=line,
                )
            )
    return findings


def _contact_findings(resume: ParsedResume) -> list[Finding]:
    findings: list[Finding] = []
    contact = resume.contact
    if not contact.email:
        findings.append(
            Finding(
                code="no_email",
                detail="no email address found; most ATS reject a record it cannot key on",
                cost=0.30,
            )
        )
    if not contact.phone:
        findings.append(Finding(code="no_phone", detail="no phone number found", cost=0.08))
    if not contact.name:
        findings.append(
            Finding(code="no_name", detail="no name found on the first lines", cost=0.15)
        )
    if not contact.links:
        findings.append(
            Finding(
                code="no_links",
                detail="no LinkedIn or GitHub link found",
                cost=0.04,
            )
        )
    return findings


def _section_findings(resume: ParsedResume) -> list[Finding]:
    findings: list[Finding] = []

    for group in _REQUIRED_EITHER:
        if any(len(resume.section(n)) >= MIN_SECTION_LINES for n in group):
            continue
        names = " or ".join(group)
        findings.append(
            Finding(
                code="missing_section",
                detail=f"no {names} section an ATS can recognize",
                cost=0.25,
            )
        )

    for name in _EXPECTED:
        if len(resume.section(name)) >= MIN_SECTION_LINES:
            continue
        findings.append(
            Finding(
                code="missing_section",
                detail=f"no {name} section an ATS can recognize",
                cost=0.10,
            )
        )

    return findings


def _date_findings(resume: ParsedResume) -> list[Finding]:
    """Dates that are absent, and dates fused to the word before them."""
    findings: list[Finding] = []

    for line in resume.raw_lines:
        if _FUSED_DATE_RE.search(line):
            findings.append(
                Finding(
                    code="fused_date",
                    detail=(
                        "a date runs into the word before it — the separator was lost in "
                        "extraction, and a parser reads one token where there are two fields"
                    ),
                    cost=0.10,
                    line=line,
                )
            )

    for name in ("experience", "education"):
        lines = resume.section(name)
        if not lines:
            continue
        if any(_DATE_RANGE_RE.search(line) for line in lines):
            continue
        findings.append(
            Finding(
                code="no_dates",
                detail=f"no date range found anywhere in {name}; an ATS files these undated",
                cost=0.15,
            )
        )

    return findings


def _bullet_findings(resume: ParsedResume) -> list[Finding]:
    """Bullets too long to be read. Only where bullets live.

    Not the summary: a summary is a paragraph and is supposed to run long. The
    owner's is 315 characters and correct.
    """
    findings: list[Finding] = []
    for name in ("experience", "projects"):
        for line in resume.section(name):
            if len(line) <= MAX_BULLET_CHARS:
                continue
            findings.append(
                Finding(
                    code="overlong_bullet",
                    detail=(
                        f"{len(line)} characters in one bullet under {name!r}; "
                        f"over {MAX_BULLET_CHARS} is where a reader stops"
                    ),
                    cost=0.02,
                    line=line,
                )
            )
    return findings


def _requirements(terms: list[str]) -> list[str]:
    """Drop posting furniture, keeping the terms that are actually asked for.

    `keywords.analyze` ranks by frequency, and what a posting repeats most is
    often its benefits and its process: against three real Machine Learning
    Engineer postings the "missing" list came back led by `Interview`,
    `minutes`, `don`, `because`, `insurance` and `paid`. Counting those as
    keywords the résumé failed to match makes the score measure the posting's
    HR section rather than its requirements.

    ## Why this filters here and not in `keywords.py`

    Widening `keywords._STOPWORDS` would fix the number and quietly weaken a
    safety control. That list is also what produces `TermReport.missing`, which
    `rewrite.vet` passes to `borrowed_terms` as the terms a rewrite may not
    take from the posting. Every word removed from it is a word the model may
    then introduce unchallenged. A measurement and a guard rail should not
    share a knob, so the filtering lives on the measurement side and the guard
    keeps the wider list.

    The vocabulary is `matching.score._BOILERPLATE`, reused rather than
    rewritten: it already answers "is this word job-posting furniture" for the
    legitimacy scorer, and a second hand-maintained copy would drift.
    """
    from packages.matching.score import _BOILERPLATE

    kept: list[str] = []
    for term in terms:
        key = normalize(term)
        # A multi-word term is a named skill — `machine learning`, `data
        # pipelines` — and is never furniture, whatever its parts look like.
        if " " in term.strip():
            kept.append(term)
            continue
        if key in _BOILERPLATE or len(key) < 3:
            continue
        kept.append(term)
    return kept


def _deduction(findings: list[Finding]) -> float:
    """Total cost, with repeats of one defect worth progressively less.

    Ten overlong bullets are one problem with the document, not ten. Summing
    flat put the owner's résumé at a floored 0% parse, where every fix would
    have shown no movement until the last one — a score that cannot go up is
    not a measurement, and this exists to be watched before and after.

    Each further instance of a code is halved: a defect still costs more when
    it is everywhere than when it is once, and the series converges to twice
    the first hit rather than growing without bound.
    """
    seen: dict[str, int] = {}
    total = 0.0
    for finding in findings:
        n = seen.get(finding.code, 0)
        total += finding.cost / (2**n)
        seen[finding.code] = n + 1
    return total


def score(resume: ParsedResume, job_description: str = "") -> AtsReport:
    """Score a parsed résumé, optionally against a posting.

    With no posting the keyword half is not computed and
    `scored_against_posting` is False — a coverage of 0.0 against no posting
    means "not asked", and reporting it as a score would read as "matches
    nothing".
    """
    findings: list[Finding] = []
    findings += _contact_findings(resume)
    findings += _section_findings(resume)
    findings += _unrecognized_headings(resume)
    findings += _date_findings(resume)
    findings += _bullet_findings(resume)

    parse = max(0.0, 1.0 - _deduction(findings))

    if not job_description.strip():
        return AtsReport(parse=round(parse, 3), keywords=0.0, findings=findings)

    terms = analyze(_requirements_text(job_description), SourceCorpus.from_resume(resume))
    supported = _requirements(terms.supported)
    missing = _requirements(terms.missing)
    total = len(supported) + len(missing)

    return AtsReport(
        parse=round(parse, 3),
        keywords=round(len(supported) / total, 3) if total else 0.0,
        findings=findings,
        supported=supported,
        missing=missing,
        scored_against_posting=True,
    )


@dataclass(frozen=True)
class AtsDelta:
    """What tailoring did to the score, for one posting.

    The pair before and after, plus the concrete answer underneath it: which
    of the posting's terms the résumé did not carry before and does now.
    `gained` is the list a person can actually check — a coverage number moving
    from 31% to 37% says nothing about whether the six terms it picked up were
    worth having.
    """

    parse_before: float
    parse_after: float
    keywords_before: float
    keywords_after: float
    #: Posting terms newly covered. Never fabricated — tailoring can only
    #: surface vocabulary the source already supported, and the guard is what
    #: holds that. These are terms that were present in the résumé and absent
    #: from the bullets an ATS reads most closely.
    gained: list[str] = field(default_factory=list)
    #: Terms the posting asks for that the résumé still does not back. Not a
    #: to-do list: closing one by writing it in would be fabrication. It is
    #: there so the owner can judge fit, and edit their own history if a gap is
    #: real and simply unwritten.
    still_missing: list[str] = field(default_factory=list)

    @property
    def parse_regressed(self) -> bool:
        """Tailoring made the document harder to parse. Always worth surfacing."""
        return self.parse_after < self.parse_before


def score_change(before: ParsedResume, after: ParsedResume, job_description: str = "") -> AtsDelta:
    """Score a résumé before and after tailoring, against the same posting.

    The measurement CLAUDE.md §7 asks for whenever the rewriter changes: a
    second referee that is not the fabrication guard's own pass rate. A run
    that raises keyword coverage while lowering the parse score has made the
    document worse in the way that matters most, and `parse_regressed` says so
    rather than leaving it to be noticed in an average.
    """
    first = score(before, job_description)
    second = score(after, job_description)

    had = {term.lower() for term in first.supported}
    return AtsDelta(
        parse_before=first.parse,
        parse_after=second.parse,
        keywords_before=first.keywords,
        keywords_after=second.keywords,
        gained=[term for term in second.supported if term.lower() not in had],
        still_missing=list(second.missing),
    )


def compare(before: AtsReport, after: AtsReport) -> str:
    """A one-line before/after, for the review screen and the benchmark.

    Both halves always shown. A rewrite that raises keyword coverage while
    lowering the parse score has made the document worse in the way that
    matters most, and a single number would hide it.
    """
    bits = [f"parse {before.parse:.0%} → {after.parse:.0%}"]
    if before.scored_against_posting and after.scored_against_posting:
        bits.append(f"keywords {before.keywords:.0%} → {after.keywords:.0%}")
    return "  ".join(bits)


__all__ = [
    "AtsDelta",
    "AtsReport",
    "Finding",
    "compare",
    "score",
    "score_change",
]
