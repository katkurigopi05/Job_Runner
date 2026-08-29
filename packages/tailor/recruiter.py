"""Would a person shortlist this résumé? A different question from the ATS one.

`packages/tailor/ats.py` asks whether a machine can read the document and
whether it carries the posting's vocabulary. Both can be perfect on a résumé
nobody would call back. The spec this project is built against separates the
two deliberately (§17, §24, §52) and warns against collapsing them into one
number, because they fail independently: a keyword-stuffed page scores well on
an ATS and badly with a human, and that is precisely the résumé an optimizer
drifts toward if only the machine is measuring.

## Four levels, because a recruiter reads in passes

§52 names them and they are genuinely different questions:

- **Ten-second scan.** Is the relevant experience obvious *without looking for
  it*? A résumé whose best evidence is on the second page loses to a worse one
  that leads with it.
- **Thirty-second qualification.** Are the accomplishments concrete? A page of
  responsibilities reads as a job description the person happened to be near.
- **Hiring-manager credibility.** Do the claims have evidence behind them? A
  Skills list naming eight technologies that appear nowhere in the experience
  is the single most common credibility problem on a real résumé.
- **Technical credibility.** Does this read as written by someone who did the
  work, or assembled from the posting?

They are reported apart and only ever averaged with the parts shown beside it.

## No model judges this

`packages/tailor/evaluate.py` sets out why an LLM-as-judge makes a bad
regression gate — the judge drifts, so a score that moves tells you nothing
about which side moved. Everything here is computed from the document: where
evidence sits, how many bullets carry a number, which listed skills are
attributed to something, how densely posting vocabulary is packed.

That buys determinism at a real cost, and the cost should be stated plainly:
this measures **the observable correlates** of what a recruiter reacts to, not
the reaction. It cannot tell whether a bullet is interesting. It can tell that
the résumé buried its only relevant experience, claimed six skills it never
evidences, and repeated the posting's words in eleven of fourteen bullets —
which is most of what makes a page get put down.

## Blind by construction

§45 says an evaluator must not know whether it is looking at an original or an
optimized document, because self-grading is how an optimizer certifies itself.
`score()` takes a résumé and a posting. There is no parameter that could carry
that fact, no field on `ParsedResume` recording it, and
`test_the_scorer_cannot_tell_an_original_from_a_rewrite` holds the signature to
it. A before/after pair is scored by two independent calls.

## Stuffing has to cost more than it gains

The failure mode worth designing against: a rewrite that injects posting
vocabulary everywhere raises ATS keyword coverage, and must not also raise
this. `_technical_credibility` penalises density and repetition hard enough
that a stuffed document scores below the one it came from — asserted in
`tests/test_recruiter.py`, not merely intended.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from packages.tailor.ats import Finding, _requirements, _requirements_text
from packages.tailor.bullets import LineKind, classify
from packages.tailor.guard import SourceCorpus, normalize
from packages.tailor.keywords import analyze
from packages.tailor.parse import ParsedResume

__all__ = ["RecruiterReport", "Shortlist", "score"]

#: Sections whose lines are the ones a recruiter actually reads for evidence.
_EVIDENCE_SECTIONS = ("experience", "projects")

#: A number, a percentage, a currency amount, or a magnitude word. The presence
#: of one is what separates "improved reporting" from "cut reporting from 40
#: minutes to 5" — the distinction §20 is about.
_QUANTIFIED = re.compile(
    r"\d|\bmillions?\b|\bbillions?\b|\bthousands?\b|\bhundreds?\b|\bdozens?\b",
    re.I,
)

#: Comfortable range for a bullet a human will actually finish. The floor is
#: not arbitrary: under it a line is a fragment rather than an accomplishment.
#: The ceiling is below `ats.MAX_BULLET_CHARS` (300) on purpose — an ATS keeps
#: a 280-character bullet and a reader abandons it halfway.
_MIN_BULLET_CHARS = 40
_MAX_BULLET_CHARS = 220

#: The share of a résumé a recruiter has read when they decide whether to keep
#: reading. Evidence below it is evidence they may never reach.
_SCAN_FRACTION = 0.4

#: A bullet with more than this share of its words drawn from the posting is
#: not describing work, it is echoing the advert.
_STUFFED_DENSITY = 0.34

#: A posting term appearing in more than this share of the evidence bullets is
#: being repeated rather than used.
_REPETITION_SHARE = 0.4


class Shortlist(str):
    """The recommendation, in the spec's own words (§17, §24)."""

    STRONG_YES = "strong yes"
    YES = "yes"
    MAYBE = "maybe"
    NO = "no"
    STRONG_NO = "strong no"


@dataclass(frozen=True)
class RecruiterReport:
    """What a human reader is likely to make of this résumé.

    Every level is 0.0–1.0 and is reported separately. `overall` exists because
    a screen needs one number; the levels are the answer.
    """

    #: Is the relevant experience obvious in the first pass?
    scan: float
    #: Are the accomplishments concrete and legible?
    qualification: float
    #: Do the claims have evidence behind them?
    credibility: float
    #: Does this read as written by someone who did the work?
    technical: float
    findings: list[Finding] = field(default_factory=list)
    #: False when no posting was supplied. `scan` and `technical` both read the
    #: posting, so without one they are not meaningful and are not scored.
    scored_against_posting: bool = False

    @property
    def overall(self) -> float:
        """Weighted toward credibility, which is the one that ends a candidacy.

        A judgement, not a measurement — which is why the levels above are the
        documented answer and this is the convenience. Credibility leads
        because a recruiter who stops believing the document stops reading it,
        and no amount of scannability recovers that.
        """
        if not self.scored_against_posting:
            return round(self.qualification * 0.5 + self.credibility * 0.5, 3)
        return round(
            self.scan * 0.2
            + self.qualification * 0.25
            + self.credibility * 0.35
            + self.technical * 0.2,
            3,
        )

    @property
    def shortlist(self) -> str:
        """The recommendation §17 asks for.

        The bands are deliberately pessimistic in the middle. A recruiter's
        default is no; "maybe" is where most real résumés sit and saying so is
        more useful than a flattering curve.
        """
        value = self.overall
        if value >= 0.85:
            return Shortlist.STRONG_YES
        if value >= 0.70:
            return Shortlist.YES
        if value >= 0.50:
            return Shortlist.MAYBE
        if value >= 0.30:
            return Shortlist.NO
        return Shortlist.STRONG_NO

    def summary(self) -> str:
        parts = [
            f"scan {self.scan:.0%}" if self.scored_against_posting else "scan n/a",
            f"qualification {self.qualification:.0%}",
            f"credibility {self.credibility:.0%}",
            f"technical {self.technical:.0%}" if self.scored_against_posting else "technical n/a",
        ]
        return f"{self.shortlist} ({self.overall:.0%}) — " + ", ".join(parts)


def _evidence_lines(resume: ParsedResume) -> list[str]:
    """The prose lines under Experience and Projects, titles excluded.

    `bullets.classify` decides, rather than a second rule here: the rewriter
    and the renderer already depend on that answer, and a third opinion about
    what a bullet is would drift against both.
    """
    lines: list[str] = []
    for section in _EVIDENCE_SECTIONS:
        lines += [
            line
            for line in resume.section(section)
            if line.strip() and classify(line) is LineKind.BULLET
        ]
    return lines


def _ten_second_scan(lines: list[str], wanted: set[str], findings: list[Finding]) -> float:
    """How much of the relevant evidence sits where it will be seen.

    Measured across the *evidence* lines rather than every line on the page.
    Counting raw lines put the cutoff inside the header: on a one-page résumé
    the name, the contact line, the section heading, the employer and the date
    range are the first five lines of thirteen, so 40% of the document ended
    before the first bullet and a perfectly well-ordered résumé scored zero.

    Evidence lines are also the ones an answer can act on. §19 permits
    reordering bullets and projects; nobody can move their name further down
    the page, so scoring them on it measures nothing they can change.
    """
    if not lines or not wanted:
        return 0.0

    cutoff = max(1, int(len(lines) * _SCAN_FRACTION))
    ordered = lines
    above = {term for term in wanted if any(term in normalize(line) for line in ordered[:cutoff])}
    below = {
        term
        for term in wanted
        if term not in above and any(term in normalize(line) for line in ordered[cutoff:])
    }
    present = above | below
    if not present:
        findings.append(
            Finding(
                code="nothing_relevant_on_top",
                detail=(
                    "none of the posting's requirements appear anywhere; a reader has "
                    "nothing to catch in the first pass"
                ),
                cost=1.0,
            )
        )
        return 0.0

    if below:
        findings.append(
            Finding(
                code="evidence_buried",
                detail=(
                    f"{len(below)} of {len(present)} matching terms appear only below the "
                    f"first {int(_SCAN_FRACTION * 100)}% of the page: "
                    + ", ".join(sorted(below)[:6])
                ),
                cost=round(len(below) / len(present), 3),
            )
        )
    return round(len(above) / len(present), 3)


def _qualification(lines: list[str], findings: list[Finding]) -> float:
    """Concrete accomplishments, in bullets a person will finish reading."""
    if not lines:
        findings.append(
            Finding(
                code="no_evidence_bullets",
                detail="no prose found under Experience or Projects",
                cost=1.0,
            )
        )
        return 0.0

    quantified = [line for line in lines if _QUANTIFIED.search(line)]
    legible = [
        line for line in lines if _MIN_BULLET_CHARS <= len(line.strip()) <= _MAX_BULLET_CHARS
    ]

    if not quantified:
        findings.append(
            Finding(
                code="nothing_measured",
                detail=(
                    "no bullet carries a number; every claim reads as a responsibility "
                    "rather than a result"
                ),
                cost=0.5,
            )
        )
    for line in lines:
        if len(line.strip()) > _MAX_BULLET_CHARS:
            findings.append(
                Finding(
                    code="bullet_too_long",
                    detail=f"{len(line.strip())} characters; a reader stops before the point",
                    cost=0.1,
                    line=line,
                )
            )

    # Both halves matter and neither substitutes for the other: metrics in
    # unreadable bullets are metrics nobody reaches.
    return round(0.6 * len(quantified) / len(lines) + 0.4 * len(legible) / len(lines), 3)


def _credibility(resume: ParsedResume, corpus: SourceCorpus, findings: list[Finding]) -> float:
    """Are the listed skills backed by anything the résumé actually describes?

    The measure is the share of the Skills section that some entry claims.
    `SourceCorpus.attributed` is exactly that set and already exists — it is
    what stops the guard letting a Skills list move a technology between
    employers, and the same distinction answers this question: a skill nothing
    is attributed to is a skill with no story behind it.

    A recruiter phrases this as "it says Kubernetes but I cannot see where they
    used it", and it is the fastest way a strong-looking résumé loses an
    interview to a technical screen.
    """
    listed = [
        token
        for line in resume.section("skills")
        for token in re.split(r"[,;|•·]", re.sub(r"^[^:]{1,40}:", "", line))
        if token.strip()
    ]
    if not listed:
        # Not a defect. Plenty of good résumés have no Skills section, and
        # scoring them zero would punish the format rather than the content.
        return 1.0

    # `.strip()` before `normalize`: it lowercases and strips punctuation but
    # not surrounding whitespace, so " FastAPI" normalizes to " fastapi" and
    # matches nothing in the corpus. That read as nine of ten skills being
    # unevidenced on a résumé that evidences most of them.
    normalized = {
        normalize(skill.strip()): skill.strip() for skill in listed if normalize(skill.strip())
    }
    if not normalized:
        return 1.0

    unevidenced = sorted(
        surface for token, surface in normalized.items() if token not in corpus.attributed
    )
    if unevidenced:
        findings.append(
            Finding(
                code="unevidenced_skills",
                detail=(
                    f"{len(unevidenced)} of {len(normalized)} listed skills appear nowhere in "
                    "the experience or projects: " + ", ".join(unevidenced[:8])
                ),
                cost=round(len(unevidenced) / len(normalized), 3),
            )
        )
    return round(1.0 - len(unevidenced) / len(normalized), 3)


def _technical_credibility(lines: list[str], wanted: set[str], findings: list[Finding]) -> float:
    """Does this read as written, or as assembled from the posting?

    Two signals, both of which get worse as an optimizer tries harder:

    - **Density.** A bullet more than a third of whose words come from the
      posting is not describing work.
    - **Repetition.** A term appearing across most of the bullets is being
      inserted rather than used.
    """
    if not lines or not wanted:
        return 1.0

    stuffed: list[str] = []
    for line in lines:
        tokens = [normalize(word) for word in line.split()]
        tokens = [token for token in tokens if token]
        if len(tokens) < 4:
            continue
        hits = sum(1 for token in tokens if token in wanted)
        if hits / len(tokens) > _STUFFED_DENSITY:
            stuffed.append(line)

    repeated = sorted(
        term
        for term in wanted
        if sum(1 for line in lines if term in normalize(line)) / len(lines) > _REPETITION_SHARE
    )

    for line in stuffed:
        findings.append(
            Finding(
                code="keyword_dense",
                detail="more than a third of this bullet is the posting's own vocabulary",
                cost=0.15,
                line=line,
            )
        )
    if repeated:
        findings.append(
            Finding(
                code="term_repeated",
                detail=(
                    f"{len(repeated)} term(s) appear in most bullets, which reads as insertion: "
                    + ", ".join(repeated[:6])
                ),
                cost=round(0.1 * len(repeated), 3),
            )
        )

    penalty = len(stuffed) / len(lines) + min(0.5, 0.15 * len(repeated))
    return round(max(0.0, 1.0 - penalty), 3)


def score(resume: ParsedResume, job_description: str = "") -> RecruiterReport:
    """Read the résumé the way a recruiter would, in four passes.

    With no posting, only the two levels that do not need one are scored —
    `scan` and `technical` both ask "relative to what this job wants", and
    answering that with no job would be inventing a reference.
    """
    corpus = SourceCorpus.from_resume(resume)
    lines = _evidence_lines(resume)
    findings: list[Finding] = []

    qualification = _qualification(lines, findings)
    credibility = _credibility(resume, corpus, findings)

    if not job_description.strip():
        return RecruiterReport(
            scan=0.0,
            qualification=qualification,
            credibility=credibility,
            technical=0.0,
            findings=findings,
        )

    # The same requirements view the ATS scorer uses, for the same reason: the
    # posting's narrative is not what the candidate is being measured against.
    terms = analyze(_requirements_text(job_description), corpus)
    wanted = {
        normalize(term)
        for term in _requirements([*terms.supported, *terms.missing])
        if normalize(term)
    }

    return RecruiterReport(
        scan=_ten_second_scan(lines, wanted, findings),
        qualification=qualification,
        credibility=credibility,
        technical=_technical_credibility(lines, wanted, findings),
        findings=findings,
        scored_against_posting=True,
    )
