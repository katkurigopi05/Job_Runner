"""Which section of a résumé the tailorer rewrites.

Every caller used to answer this by writing `section("experience")` inline —
the extractor in `apply_job`, the one in `batch`, the one in `compare`, and the
write-back in `publish`. Four copies of a decision that must agree, and
`publish.TAILORED_SECTION` already carried a note saying so.

They agreed on the wrong answer for a whole class of résumé. A student, a new
graduate, or a career changer has no employment section: the substance is under
Projects. `section("experience")` returns nothing, `_tailor` returns `None`, and
the entire Phase 3 pipeline is a silent no-op — the owner sees a review screen
with no diff and no reason given, and the employer receives the base résumé.

## Why Projects is a safe fallback

§2.1 is *more* permissive there, not less: the Projects section may carry facts
verified by GitHub's source-reported name, description, language and topics,
provided they stay attributed to that project. Rewriting a project bullet is the
same operation as rewriting an employment bullet and is held to the same guard —
and `guard._ATTRIBUTED_SECTIONS` already lists `projects`, so a rewrite is
scoped to its own project and cannot borrow from a sibling.

## Experience wins when both exist

Order matters and is not alphabetical. A résumé with both sections is an
employment résumé whose projects are supporting material; rewriting the projects
and leaving the jobs untouched would tailor the half the employer reads second.
"""

from __future__ import annotations

import re
from enum import StrEnum

from packages.tailor.parse import ParsedResume

#: Sections the tailorer may rewrite, in priority order.
TAILORABLE_SECTIONS: tuple[str, ...] = ("experience", "projects")

#: A repository or portfolio annotation. A line carrying one is naming a
#: project, not describing work done on it.
_LINK_MARKER_RE = re.compile(
    r"\[[^\]]{1,20}\]|https?://|(?:github|gitlab|linkedin)\.com/|\bwww\.", re.IGNORECASE
)

#: A date range — the shape of an employment or education entry line.
_DATE_RANGE_RE = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\w*\.?\s+(?:19|20)\d{2}\b"
    r"|\b(?:19|20)\d{2}\s*[-–—]\s*(?:(?:19|20)\d{2}|present|current)\b",
    re.IGNORECASE,
)

#: Evidence that a line makes a statement rather than naming a thing.
#: Deliberately crude — a participle or a gerund is what a résumé bullet opens
#: with, and one anywhere in the line is enough.
#: A verb form, and deliberately **case-sensitive**: only a lowercase one.
#:
#: The docstring on `classify` already makes this argument for the *opening*
#: word — "`-ing` is a gerund as often as a verb" — and the final fallback
#: below was still matching anywhere, case-insensitively. So the gerund inside
#: an ordinary job title fired it: `Software Engineering Intern, Acme Corp`,
#: `Engineering Manager`, `Data Engineering Lead`, `Machine Learning Engineer`
#: and `Marketing Analyst` were all filed as bullets.
#:
#: That is not a typography complaint. `is_rewritable` is this same answer, so
#: the model was being asked to rewrite those job titles — the defect this
#: module was written to stop, reappearing on a line it could not see, and on
#: a line that names an employer and a role.
#:
#: Case is the signal that separates the two. A gerund inside a name is
#: capitalized (`Machine Learning`, `Software Engineering`); a verb in a
#: sentence is not, unless it opens the line — and `_opens_with_a_verb` has
#: already had its say by the time this runs.
_VERBAL_RE = re.compile(r"\b[a-z]\w{2,}(?:ed|ing)\b")

#: Irregular past tenses a résumé bullet opens with that `-ed` misses.
_IRREGULAR_PAST_TEXT = """
built wrote ran led made drove grew set sent kept taught won chose brought
gave took found held rebuilt spent met beat cut put shipped drew
"""

_IRREGULAR_PAST: frozenset[str] = frozenset(_IRREGULAR_PAST_TEXT.split())

#: Above this, a line is prose whatever else it looks like.
_PROSE_WORDS = 12

#: A comma-separated line whose fragments are this short is a list of things,
#: not a sentence about them.
_LIST_FRAGMENT_WORDS = 4


def tailorable_section(parsed: ParsedResume) -> str | None:
    """The section holding the bullets to rewrite, or None if there are none.

    None means there is nothing to tailor — not that something failed. A résumé
    with neither section is one the parser could not read, or one that genuinely
    has no prose to rewrite, and both are the caller's to report.
    """
    for name in TAILORABLE_SECTIONS:
        if any(line.strip() for line in parsed.section(name)):
            return name
    return None


class LineKind(StrEnum):
    """What a line inside an entry-bearing section is."""

    #: Prose describing work done. The only kind the model rewrites.
    BULLET = "bullet"
    #: The line that starts an entry — a project name, an employer and role,
    #: a degree and its dates.
    ENTRY = "entry"
    #: A supporting line under an entry name, in practice a technology list.
    META = "meta"


def classify(line: str) -> LineKind:
    """What kind of line this is.

    A section is not a list of bullets. It is a list of *entries*, each opening
    with a name, often carrying a technology line, and only then having prose.
    Two callers need the distinction and would otherwise each guess at it: the
    rewriter, which must not be asked to tailor a project title, and the
    renderer, which cannot lay out an entry it cannot find.

    On the owner's résumé, 12 of the 28 lines under Projects are not bullets:
    six titles like `Attorney.AI — Citation-First Legal Research RAG Assistant
    [GitHub]` and six stacks like `Python, FastAPI, React, HuggingFace
    Transformers, Qdrant`. The model was asked to rewrite every one, which is
    most of why tailored output read badly.

    ## The default is BULLET

    Only positively identified non-prose is excluded, and each test is chosen
    to be high-precision rather than broad. The opposite default is the failure
    CLAUDE.md already records twice — a tailorer that silently does nothing —
    and it is harder to notice than a bad rewrite, because a bad rewrite is
    visible on the review screen and an absent one is not.

    Excluding a real bullet costs a rewrite. Including a title costs a title
    rewritten into a sentence, which the owner then has to catch. Neither is
    free, so the tests below identify what a line *is*, not what it is not.
    """
    stripped = line.strip()
    if not stripped:
        return LineKind.META

    # A leading bullet glyph is the author saying which lines are bullets.
    # Nothing beats being told.
    if stripped[0] in "-•*‣◦–—" and len(stripped) > 2:
        return LineKind.BULLET

    # A repository or portfolio annotation names a thing, at any length. This
    # is the one exception to "a long line is prose" below, and it is here
    # rather than after the length test because `[GitHub]` is only ever put on
    # a title.
    if _LINK_MARKER_RE.search(stripped):
        return LineKind.ENTRY

    # A date range says "entry" — unless the line opens like a bullet. Both
    # readings are common and the opening word separates them: an employment
    # header is a noun phrase (`Senior Software Engineer, Acme Corp, Jan 2021 -
    # Present`), while a bullet that happens to mention a span is still a
    # sentence (`Led the platform migration from 2019 - 2021 and cut deploy
    # time by half`). Without this the second was filed as an entry, skipped by
    # the rewriter and rendered with entry-name typography.
    #
    # Testing the length instead would be the wrong cut: that header is
    # thirteen words and is not prose.
    if _DATE_RANGE_RE.search(stripped) and not _opens_with_a_verb(stripped):
        return LineKind.ENTRY

    words = stripped.split()
    if len(words) < _PROSE_WORDS and _looks_like_a_label(stripped):
        return LineKind.ENTRY

    # The opening word settles it, and settles it before the comma test below.
    # Prose commas fool that test on their own: `Added model adapters,
    # audio-upload handling, and graceful fallbacks.` is three short
    # comma-separated fragments and is plainly a bullet.
    #
    # *Opening* rather than anywhere, because `-ing` is a gerund as often as a
    # verb: `Python, Scikit-learn, Time-Series Forecasting` is a stack list
    # whose last word looks like an action and is not one. A résumé bullet
    # opens with its verb — that convention is the signal.
    # `Networking Virtual Intern, EduSkills Foundation` opens with a gerund and
    # is a job title. So do `Engineering Manager, Acme` and `Marketing Analyst,
    # Delta`. The opening-verb convention above is a good signal and these are
    # the exception to it: the gerund is a noun, and the line is a name.
    #
    # `_looks_like_a_label` cannot rescue them because it rejects any line
    # containing a comma, and `Role, Employer` is the shape every job title
    # takes. Rather than widen that test — it guards an earlier and broader
    # branch — this one is deliberately narrow: every word capitalized, a
    # comma present, and no terminal full stop. A bullet fails it on its first
    # lowercase word, which it reaches almost immediately.
    if _reads_as_role_and_employer(stripped):
        return LineKind.ENTRY

    if _opens_with_a_verb(stripped):
        return LineKind.BULLET
    if len(words) >= _PROSE_WORDS:
        return LineKind.BULLET
    if _is_list(stripped):
        return LineKind.META
    # A verb further in, on a line short enough to have got here: prose with an
    # unusual opening rather than a name.
    return LineKind.BULLET if _VERBAL_RE.search(stripped) else LineKind.ENTRY


def is_rewritable(line: str) -> bool:
    """Whether the model should be asked to rewrite this line."""
    return bool(line.strip()) and classify(line) is LineKind.BULLET


def _opens_with_a_verb(line: str) -> bool:
    """Whether the line opens the way a résumé bullet does."""
    words = line.split()
    if not words:
        return False
    first = words[0].strip(".,:;").lower()
    return bool(re.fullmatch(r"\w{3,}(?:ed|ing)", first)) or first in _IRREGULAR_PAST


def _looks_like_a_label(line: str) -> bool:
    """ALL CAPS, or Title Case With Every Word Capitalized.

    A heading the parser did not claim, or an entry name. Checked before the
    verb test because a name can contain one: `Selected Work` opens with a past
    participle and is not a bullet, and neither is `Cloud Data Warehousing`.

    A résumé bullet fails this on its first lowercase word, which it reaches
    almost immediately — `Built the ingest path`.
    """
    if any(c in ",[]()0123456789/:" for c in line) or line.endswith("."):
        return False
    letters = [c for c in line if c.isalpha()]
    if not letters:
        return False
    if all(c.isupper() for c in letters):
        return True
    return all(word[0].isupper() for word in line.split() if word and word[0].isalpha())


#: Lowercase words a title may contain without ceasing to be a title.
_TITLE_CONNECTORS = frozenset("a an and at by for from in of on or the to with via".split())


def _reads_as_role_and_employer(line: str) -> bool:
    """`Networking Virtual Intern, EduSkills Foundation` — a name, not prose.

    Three conditions together, because any one alone is wrong:

    - **A comma.** This is the `Role, Employer` shape. Without it,
      `Built Machine Learning Models` would qualify, and that is a bullet.
    - **No terminal full stop.** A bullet is a sentence and punctuates like one.
    - **Every word capitalized**, allowing the small connectors a title keeps
      in lowercase. This is what a bullet fails, usually on its second word.
    """
    # A technology line is also title-cased and comma-separated —
    # `Python, Postgres, Kubernetes` — and is supporting material, not a name.
    # `_is_list` already draws that line for the branch further down; asking it
    # here rather than inventing a second notion of "list" keeps the two from
    # disagreeing.
    if "," not in line or line.rstrip().endswith(".") or _is_list(line):
        return False
    words = [w for w in line.split() if w and w[0].isalpha()]
    if len(words) < 2:
        return False
    return all(word[0].isupper() or word.strip(".,").lower() in _TITLE_CONNECTORS for word in words)


def _is_list(line: str) -> bool:
    """A run of short comma-separated fragments — a stack, not a sentence.

    `Python, FastAPI, React, HuggingFace Transformers, Qdrant` and `Snowflake,
    AWS Redshift, Google BigQuery, Talend, Tableau, SQL`. Requires at least two
    commas, because one comma is ordinary punctuation in a sentence, and every
    fragment to be short, because `Built X, which did Y across Z` has long ones.
    """
    fragments = [part.strip() for part in line.split(",")]
    if len(fragments) < 3:
        return False
    return all(0 < len(part.split()) <= _LIST_FRAGMENT_WORDS for part in fragments)


def rewritable_indices(lines: list[str]) -> list[int]:
    """Positions in a section's lines that the model should be handed.

    Positions rather than the lines themselves, because the write-back in
    `publish.apply_rewrites` has to skip exactly the same lines in exactly the
    same order. Both callers ask this one function; if they disagreed, a
    rewrite would land on the line after the one it was written for, silently.
    """
    return [i for i, line in enumerate(lines) if is_rewritable(line)]


def tailorable_bullets(parsed: ParsedResume) -> tuple[str | None, list[str]]:
    """The section name and the prose lines within it.

    Returned together so a caller cannot rewrite bullets taken from one section
    and write them back into another — the failure this module exists to make
    impossible.
    """
    name = tailorable_section(parsed)
    if name is None:
        return None, []
    lines = parsed.section(name)
    return name, [lines[i] for i in rewritable_indices(lines)]
