"""What the finished PDF says, read back out of it.

Every check upstream of here reads the résumé as structure: the guard reads a
bullet, the scorer reads sections. An employer's parser reads neither. It
reads the text layer of the file that was uploaded, and a term the tailorer
added is worth something only if it is in that text, whole, with the facts
that were beside it.

The owner's fork of `ai-job-search` makes this its step 5d
(`.claude/commands/apply.md`): extract the text, check it parses, check each
keyword against what was extracted and not against the source. The list of
things worth checking is taken from there. The code is this project's own
extractor, scorer and layout measurement.

Two halves:

- `check_pdf` asks whether one PDF holds what it was meant to: the terms a
  revision added, every figure and listed technology of its bullets, text
  that is text, and where it sits on its pages.
- `compare` puts two résumés side by side **in the same representation**:
  the base and the revision as documents, or the base and the revision each
  rendered by the same template and read back from its PDF. A base résumé as
  the owner uploaded it against a revision as this project rendered it is
  two templates, and the difference is the template's.

The scores are heuristics about how a parser and a reader are likely to take
a document. None predicts a hiring decision, and nothing here selects or
attaches anything: a regression is listed for the owner to look at.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from packages.tailor import ats, figures, recruiter
from packages.tailor.bullets import tailorable_bullets
from packages.tailor.guard import _TOKEN_RE, _index, normalize, singular
from packages.tailor.layout import measure, section_headings
from packages.tailor.parse import ParsedResume, extract_text, parse_resume
from packages.tailor.targeted import Said, _document, posting_terms
from packages.tailor.technologies import _separated, inventory

#: What a text layer that is not text looks like: an unmapped glyph, or the
#: replacement character.
_GARBLED = ("(cid:", "�")

#: A score is a heuristic and is rounded to three places everywhere it is
#: made. Less than this is not a fall.
_TOLERANCE = 0.0005


@dataclass(frozen=True)
class PdfCheck:
    """One PDF, held to what it was meant to contain."""

    #: Characters of text the extractor found. Zero is an image of a résumé.
    extracted_chars: int
    garbled: bool
    #: Terms the revision added that the extracted text does not say.
    missing_terms: tuple[str, ...]
    #: Figures and listed technologies of the résumé's bullets that the
    #: extracted text does not have, as the bullets wrote them.
    missing_figures: tuple[str, ...]
    missing_technologies: tuple[str, ...]
    #: The parse half of the ATS score, on the résumé parsed back from the PDF.
    parse: float
    parse_findings: tuple[str, ...]
    pages: int
    layout_findings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """The text is there and holds every term and fact it should.

        Parse and layout findings are not in this: a base résumé can have its
        own, and `compare` is what says whether a revision added one.
        """
        return (
            self.extracted_chars > 0
            and not self.garbled
            and not (self.missing_terms or self.missing_figures or self.missing_technologies)
        )


def check_pdf(pdf: bytes, resume: ParsedResume, *, terms: Sequence[str] = ()) -> PdfCheck:
    """Read `pdf` back and hold it to `resume`, the document it was rendered from."""
    text = extract_text(pdf, "resume.pdf")
    flat = " ".join(text.split())
    said = Said(flat)
    present = _index(_separated(flat))
    values = figures.stated(flat)

    _, bullets = tailorable_bullets(resume)
    listed = inventory(resume)
    lost_figures: list[str] = []
    lost_technologies: list[str] = []
    for bullet in bullets:
        for figure in figures.read(bullet):
            if figure.key not in values and figure.text not in lost_figures:
                lost_figures.append(figure.text)
        for raw in _TOKEN_RE.findall(_separated(bullet)):
            word = normalize(raw)
            if word not in listed or word in present or singular(word) in present:
                continue
            shown = raw.strip(".,;:")
            if shown not in lost_technologies:
                lost_technologies.append(shown)

    parsed_back = parse_resume(pdf, "resume.pdf")
    read = ats.score(parsed_back)
    layout = measure(pdf, headings=section_headings(resume.sections))

    return PdfCheck(
        extracted_chars=len(flat),
        garbled=any(mark in text for mark in _GARBLED),
        missing_terms=tuple(term for term in terms if not said.says(term)),
        missing_figures=tuple(lost_figures),
        missing_technologies=tuple(lost_technologies),
        parse=read.parse,
        parse_findings=tuple(finding.code for finding in read.findings),
        pages=layout.pages if layout is not None else 0,
        layout_findings=tuple(finding.code for finding in layout.findings) if layout else (),
    )


@dataclass(frozen=True)
class Reading:
    """How one résumé reads against one posting. Every number is a heuristic."""

    parse: float
    #: Posting terms the résumé backs, in any spelling: what `ats.score` and
    #: the review screen count.
    backed: int
    backed_of: int
    #: Posting terms the résumé says in the posting's own words.
    stated: int
    stated_of: int
    recruiter: float
    scan: float
    qualification: float
    credibility: float
    technical: float


def read(resume: ParsedResume, job_description: str) -> Reading:
    """Score `resume` as it would be printed.

    A copy is scored whose `raw_lines` are its sections. A tailored résumé's
    `raw_lines` are still the source's, on purpose, and a phrase is looked for
    in them: scored as it is stored, a revision's new phrase would not count
    and the base's would.
    """
    printed = resume.model_copy(update={"raw_lines": _document(resume).split("\n")})
    scored = ats.score(printed, job_description)
    reader = recruiter.score(printed, job_description)
    said = Said(_document(resume))
    terms = posting_terms(job_description)
    return Reading(
        parse=scored.parse,
        backed=len(scored.supported),
        backed_of=len(scored.supported) + len(scored.missing),
        stated=sum(1 for term in terms if said.says(term)),
        stated_of=len(terms),
        recruiter=reader.overall,
        scan=reader.scan,
        qualification=reader.qualification,
        credibility=reader.credibility,
        technical=reader.technical,
    )


@dataclass(frozen=True)
class Comparison:
    """Two résumés in one representation, and what got worse between them."""

    #: What was compared: `document`, or `pdf` for both read back from a
    #: rendering by the same template.
    representation: str
    before: Reading
    after: Reading

    @property
    def regressions(self) -> tuple[str, ...]:
        fell: list[str] = []
        for name in ("parse", "recruiter", "scan", "qualification", "credibility", "technical"):
            was, now = getattr(self.before, name), getattr(self.after, name)
            if now < was - _TOLERANCE:
                fell.append(f"{name} {was:.0%} to {now:.0%}")
        for name in ("backed", "stated"):
            was, now = getattr(self.before, name), getattr(self.after, name)
            if now < was:
                fell.append(f"{name} terms {was} to {now}")
        return tuple(fell)


def compare(
    before: ParsedResume, after: ParsedResume, job_description: str, *, representation: str
) -> Comparison:
    return Comparison(
        representation=representation,
        before=read(before, job_description),
        after=read(after, job_description),
    )


def compare_pdfs(before: bytes, after: bytes, job_description: str) -> Comparison:
    """Both PDFs parsed back and scored. They must come from one template."""
    return compare(
        parse_resume(before, "resume.pdf"),
        parse_resume(after, "resume.pdf"),
        job_description,
        representation="pdf",
    )


def new_findings(before: PdfCheck, after: PdfCheck) -> tuple[str, ...]:
    """Parse and layout findings the revision's PDF has and the base's does not."""
    added: list[str] = []
    for label, was, now in (
        ("parse", before.parse_findings, after.parse_findings),
        ("layout", before.layout_findings, after.layout_findings),
    ):
        remaining = list(was)
        for code in now:
            if code in remaining:
                remaining.remove(code)
            else:
                added.append(f"{label}: {code}")
    if after.pages > before.pages:
        added.append(f"layout: {before.pages} pages to {after.pages}")
    return tuple(added)


__all__ = [
    "Comparison",
    "PdfCheck",
    "Reading",
    "check_pdf",
    "compare",
    "compare_pdfs",
    "new_findings",
    "read",
]
