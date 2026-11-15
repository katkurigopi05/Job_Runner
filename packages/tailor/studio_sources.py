"""The lines a Resume Studio draft adds to a résumé, and what each line may draw on.

The studio starts from a base résumé the owner uploaded and adds only what
they stated and confirmed on the dashboard (`packages/core/models_evidence.py`):

- **An internship** becomes an Experience entry: a heading line, its tools,
  and its bullets as the owner wrote them. The tools are evidence for that
  internship's own lines and no other's (`guard.SourceCorpus.from_resume`,
  `verified`), so a rewrite may say an internship used FastAPI only when the
  owner said it did.
- **A certification** becomes a Certifications line, and the skills it covers
  that the Skills section does not already list become one Skills line. A
  skill in Skills counts as covered for a posting term, so targeted tailoring
  never rewrites a bullet to claim it (`targeted.classify`).

Nothing here writes a word the owner did not type. An internship or a
certification already on the base résumé is left where it is and reported as
skipped, so nothing is listed twice. `raw_lines` keeps the base's lines and
appends the added ones, because it is what the guard treats as "was this in
the source".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from packages.tailor.bullets import LineKind, classify
from packages.tailor.guard import line_key, normalize
from packages.tailor.parse import ParsedResume
from packages.tailor.skills import _split_label, _split_skills

#: The Skills label the certified skills are listed under.
CERTIFIED_LABEL = "Certified:"

EXPERIENCE = "experience"
CERTIFICATIONS = "certifications"
SKILLS = "skills"


@dataclass(frozen=True)
class InternshipFacts:
    """One confirmed internship, as the owner typed it."""

    id: str
    company: str
    role: str
    started: str
    ended: str | None = None
    location: str | None = None
    stack: tuple[str, ...] = ()
    bullets: tuple[str, ...] = ()


@dataclass(frozen=True)
class CertificationFacts:
    """One confirmed certification, as the owner typed it."""

    id: str
    name: str
    issuer: str
    issued: str | None = None
    expires: str | None = None
    credential_id: str | None = None
    skills: tuple[str, ...] = ()


@dataclass(frozen=True)
class Origin:
    """Where one line of the draft came from."""

    #: `resume`, `internship` or `certification`.
    kind: str
    #: The company or certification it belongs to; empty for the base résumé.
    label: str = ""


@dataclass(frozen=True)
class Source:
    """The base résumé with the owner's confirmed additions, before any model is asked."""

    resume: ParsedResume
    #: Experience line (as written) to the tools confirmed for its internship.
    verified: Mapping[str, str] = field(default_factory=dict)
    #: `guard.line_key` of an added line to where it came from. A line not
    #: here is the base résumé's.
    origins: Mapping[str, Origin] = field(default_factory=dict)
    added: tuple[str, ...] = ()
    #: Records left out because the base résumé already lists them.
    skipped: tuple[str, ...] = ()

    def origin_of(self, line: str) -> Origin:
        return self.origins.get(line_key(line), Origin("resume"))


def _clean(values: Sequence[str]) -> list[str]:
    return [value.strip() for value in values if value and value.strip()]


def _dates(started: str | None, ended: str | None) -> str:
    """`started - ended` as written, or `started` alone. An open end is not called "Present"."""
    first, last = (started or "").strip(), (ended or "").strip()
    return f"{first} - {last}" if first and last else first or last


def internship_lines(internship: InternshipFacts) -> list[str]:
    """Heading, tools and bullets. Two spaces before the dates, which the renderer splits on."""
    heading = ", ".join(_clean([internship.role, internship.company, internship.location or ""]))
    dates = _dates(internship.started, internship.ended)
    lines = [f"{heading}  {dates}" if dates else heading]
    stack = _clean(internship.stack)
    if stack:
        lines.append(", ".join(stack))
    return lines + _clean(internship.bullets)


def certification_line(certification: CertificationFacts) -> str:
    """Name, issuer, credential and dates on one line, so it reads as one entry."""
    line = ", ".join(_clean([certification.name, certification.issuer]))
    if certification.credential_id and certification.credential_id.strip():
        line = f"{line} (Credential ID {certification.credential_id.strip()})"
    dates = _dates(certification.issued, certification.expires)
    return f"{line}  {dates}" if dates else line


def _lists_internship(resume: ParsedResume, internship: InternshipFacts) -> bool:
    """Whether an Experience heading already names this company."""
    company = internship.company.strip().casefold()
    return bool(company) and any(
        company in line.casefold()
        for line in resume.section(EXPERIENCE)
        if classify(line) is not LineKind.BULLET
    )


def _lists_certification(resume: ParsedResume, certification: CertificationFacts) -> bool:
    name = certification.name.strip().casefold()
    lines = [*resume.section(CERTIFICATIONS), *resume.section("licenses")]
    return bool(name) and any(name in line.casefold() for line in lines)


def _listed_skills(resume: ParsedResume) -> set[str]:
    """Every skill the Skills section names, normalized."""
    listed: set[str] = set()
    for line in resume.section(SKILLS):
        _, _, body = _split_label(line)
        listed |= {normalize(item) for item in (_split_skills(body) or [body])}
    return listed


def certified_skills_line(
    resume: ParsedResume, certifications: Sequence[CertificationFacts]
) -> str:
    """One Skills line of the certified skills Skills does not list yet. Empty when none."""
    listed = _listed_skills(resume)
    new: list[str] = []
    for certification in certifications:
        for skill in _clean(certification.skills):
            key = normalize(skill)
            if key and key not in listed:
                listed.add(key)
                new.append(skill)
    return f"{CERTIFIED_LABEL} {', '.join(new)}" if new else ""


def build_source(
    base: ParsedResume,
    internships: Sequence[InternshipFacts] = (),
    certifications: Sequence[CertificationFacts] = (),
) -> Source:
    """The base résumé with the confirmed internships and certifications added. Pure."""
    resume = base.model_copy(deep=True)
    origins: dict[str, Origin] = {}
    verified: dict[str, str] = {}
    added: list[str] = []
    skipped: list[str] = []
    appended: list[str] = []

    def append(section: str, lines: list[str], origin: Origin) -> None:
        if section not in resume.sections:
            appended.append(section)
        resume.sections[section] = [*resume.section(section), *lines]
        appended.extend(lines)
        origins.update({line_key(line): origin for line in lines})

    for internship in internships:
        label = f"{internship.role}, {internship.company}"
        if _lists_internship(base, internship):
            skipped.append(label)
            continue
        lines = internship_lines(internship)
        append(EXPERIENCE, lines, Origin("internship", internship.company))
        stack = ", ".join(_clean(internship.stack))
        if stack:
            verified.update({line: stack for line in lines})
        added.append(label)

    kept = []
    for certification in certifications:
        if _lists_certification(base, certification):
            skipped.append(certification.name)
            continue
        kept.append(certification)
        append(
            CERTIFICATIONS,
            [certification_line(certification)],
            Origin("certification", certification.name),
        )
        added.append(certification.name)

    skills_line = certified_skills_line(base, kept)
    if skills_line:
        append(SKILLS, [skills_line], Origin("certification", "certified skills"))

    resume.raw_lines = [*base.raw_lines, *appended]
    return Source(
        resume=resume,
        verified=verified,
        origins=origins,
        added=tuple(added),
        skipped=tuple(skipped),
    )


def same_skills(line: str, source_line: str) -> bool:
    """Two Skills lines that list the same skills under the same label, in any order.

    Tailoring reorders a Skills line to put a posting's skills first
    (`skills.reorder_skills`), so the line it returns is the source's without
    being equal to it.
    """
    label, _, body = _split_label(line)
    source_label, _, source_body = _split_label(source_line)
    items = _split_skills(body)
    source_items = _split_skills(source_body)
    if items is None or source_items is None or label.strip() != source_label.strip():
        return False
    return sorted(i.strip() for i in items) == sorted(i.strip() for i in source_items)
