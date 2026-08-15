"""Parse a résumé file into structured JSON.

This is extraction, not interpretation. Every string in the output appears
verbatim in the source document — nothing is summarized, normalized, or
inferred. That property is what later phases depend on: the fabrication guard
(§2.1) checks tailored output against this structure, so if the parser
paraphrased, the guard would be checking against a paraphrase.

Section detection is heuristic and deliberately conservative. Text that does
not clearly belong to a known section lands in `preamble` or the previous
section rather than being dropped — losing a line from someone's résumé is
worse than filing it imperfectly.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

from pydantic import BaseModel, Field

#: Canonical section names and the headings that map to them. Order matters
#: only for reporting; matching is by longest heading first.
SECTION_PATTERNS: dict[str, tuple[str, ...]] = {
    "summary": ("summary", "objective", "profile", "about me", "professional summary"),
    "experience": (
        "experience",
        "work experience",
        "employment",
        "employment history",
        "professional experience",
        "work history",
    ),
    "education": ("education", "academic background", "academics"),
    "skills": ("skills", "technical skills", "core competencies", "technologies"),
    "projects": ("projects", "personal projects", "selected projects", "side projects"),
    "certifications": ("certifications", "certificates", "licenses"),
    "publications": ("publications", "papers"),
    "awards": ("awards", "honors", "achievements"),
    "languages": ("languages",),
    "interests": ("interests", "hobbies"),
}

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Two shapes, tried in order: an international number (leading +, country
# code, 7-14 more digits) and the North American 3-3-4 grouping. Résumés carry
# both, and a résumé from outside North America is not an edge case.
_PHONE_RE = re.compile(
    r"\+\d{1,3}[\s.\-()]{0,2}(?:\d[\s.\-()]{0,2}){6,13}\d"
    r"|\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}"
)
_URL_RE = re.compile(r"https?://[^\s,;)]+|(?:www\.|linkedin\.com|github\.com)[^\s,;)]+")

#: A heading line is short and has no sentence punctuation.
_MAX_HEADING_WORDS = 5


class ParseError(Exception):
    """The file could not be read as a résumé."""


class Contact(BaseModel):
    """Contact details found in the document, verbatim."""

    name: str | None = None
    email: str | None = None
    phone: str | None = None
    links: list[str] = Field(default_factory=list)


class ParsedResume(BaseModel):
    """A résumé as structure, with every string traceable to the source."""

    contact: Contact = Field(default_factory=Contact)
    #: Anything above the first recognized heading.
    preamble: list[str] = Field(default_factory=list)
    #: Canonical section name → its lines, in document order.
    sections: dict[str, list[str]] = Field(default_factory=dict)
    #: Every line, in order. The authority for "was this in the source?".
    raw_lines: list[str] = Field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.raw_lines)

    def section(self, name: str) -> list[str]:
        return self.sections.get(name, [])


# --------------------------------------------------------------------------
# Text extraction
# --------------------------------------------------------------------------


def extract_text(data: bytes, filename: str) -> str:
    """Pull plain text out of a résumé file.

    Supports PDF, DOCX, and plain text — the three things a résumé actually
    arrives as. A .doc (old binary Word) is rejected rather than mangled.
    """
    suffix = Path(filename).suffix.lower()

    if suffix == ".pdf":
        return _extract_pdf(data)
    if suffix == ".docx":
        return _extract_docx(data)
    if suffix in (".txt", ".md", ""):
        return data.decode("utf-8", errors="replace")
    if suffix == ".doc":
        raise ParseError("legacy .doc is not supported; save as .docx or PDF")
    raise ParseError(f"unsupported résumé format: {suffix or 'unknown'}")


def _extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:  # noqa: BLE001 - any malformed PDF lands here
        raise ParseError(f"could not read PDF: {type(exc).__name__}") from exc

    text = "\n".join(pages)
    if not text.strip():
        raise ParseError(
            "no text found in PDF — it may be a scan. Export a text-based PDF; "
            "an ATS cannot read a scanned one either."
        )
    return text


def _extract_docx(data: bytes) -> str:
    import docx

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise ParseError(f"could not read DOCX: {type(exc).__name__}") from exc

    lines = [p.text for p in document.paragraphs]
    # Tables are common in résumé templates and easy to lose.
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                lines.append("  ".join(cells))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Structure
# --------------------------------------------------------------------------


def _normalize_heading(line: str) -> str:
    return re.sub(r"[^a-z ]", "", line.lower()).strip()


def _match_section(line: str) -> str | None:
    """Return the canonical section this line is a heading for, if any."""
    stripped = line.strip()
    if not stripped or len(stripped.split()) > _MAX_HEADING_WORDS:
        return None
    # A line ending in a period is prose, not a heading.
    if stripped.endswith((".", ",", ";")):
        return None

    normalized = _normalize_heading(stripped)
    if not normalized:
        return None

    for canonical, headings in SECTION_PATTERNS.items():
        if normalized in headings:
            return canonical
    return None


def _find_contact(lines: list[str]) -> Contact:
    """Contact details, taken verbatim from wherever they appear.

    The name heuristic is the first non-empty line that is not itself contact
    data — conventional for résumés, and wrong often enough that it stays
    editable in the profile rather than being treated as authoritative.
    """
    blob = "\n".join(lines)

    email_match = _EMAIL_RE.search(blob)
    phone_match = _PHONE_RE.search(blob)

    links: list[str] = []
    for match in _URL_RE.finditer(blob):
        url = match.group(0).rstrip(".,;")
        if url not in links:
            links.append(url)

    name = None
    for line in lines[:5]:
        candidate = line.strip()
        if not candidate:
            continue
        if _EMAIL_RE.search(candidate) or _PHONE_RE.search(candidate):
            continue
        if _URL_RE.search(candidate):
            continue
        if len(candidate.split()) <= 5:
            name = candidate
            break

    return Contact(
        name=name,
        email=email_match.group(0) if email_match else None,
        phone=phone_match.group(0).strip() if phone_match else None,
        links=links,
    )


def parse_text(text: str) -> ParsedResume:
    """Segment already-extracted text into sections."""
    raw_lines = [line.rstrip() for line in text.splitlines()]
    non_empty = [line for line in raw_lines if line.strip()]

    resume = ParsedResume(contact=_find_contact(non_empty), raw_lines=non_empty)

    current: str | None = None
    for line in non_empty:
        matched = _match_section(line)
        if matched is not None:
            current = matched
            resume.sections.setdefault(current, [])
            continue

        if current is None:
            resume.preamble.append(line)
        else:
            resume.sections[current].append(line)

    return resume


def parse_resume(data: bytes, filename: str) -> ParsedResume:
    """Extract and segment a résumé file."""
    return parse_text(extract_text(data, filename))
