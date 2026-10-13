"""A tool a bullet names, and the practice it is a tool for.

A posting asks for "automated testing". The résumé never says it. One bullet
says "Loaded 1,000 billing records with pytest checks." The owner's fork of
`ai-job-search` calls that term *missing (have it)*: the candidate has the
skill and the CV never says so. Here it is the fourth thing a posting's term
can be, between an alias and a gap, and `targeted.classify` reads it.

## Why this is not the alias table, and why it is allowed

`aliases.py` refuses exactly this pair. `git` and `version control`, `docker`
and `containers` are "a tool" and "an implementation": each is an example of
the other, and a résumé showing one does not show the other. That holds for
an alias, because an alias goes both ways and applies to the whole document.
"Automated testing" does not show pytest.

One way it does hold. A bullet that ran pytest checks did automated testing:
the second statement says less than the first and is true whenever the first
is. So a practice may be written **beside its tool**, and nowhere else.

## What counts as evidence

**A statement, not a name.** The first version of this read the practice into
any bullet that named the tool. A name is a mention. "Skills: pytest" lists
it, "Planning to learn pytest" hopes for it, "Did not use pytest" denies it,
and all three named it. `affirmed_in` asks for more, in the one line:

- the line is a bullet, a sentence about work, and not a list;
- it names the tool, with a word for what the tool was used for beside it
  ("pytest checks", "tested with pytest", "a pytest suite");
- nothing just before or after denies it, and nothing before it makes it a
  plan.

Each test is a short list of words and a window of a few of them. That is
crude, and it is crude in one direction: a bullet it cannot read is one whose
evidence is not counted, and the term stays a gap.

**In that bullet, and kept there.** `targeted.py` makes the term a target of
the bullet that says it and off limits to every other, so a side project's
pytest never puts "automated testing" on an employer's bullet. A revision
that is kept still has every word of the bullet, the tool among them
(`targeted.taken_away`), has gained no word but the term
(`targeted.brought_in`), and has the practice standing beside its tool
(`stands_beside`): the claim keeps the scope it had.

**One way, and only when asked.** A tool is evidence of its practice and a
practice of nothing. The ordinary rewrite does not read this table. There the
term is one the résumé does not state, it goes to the model as off limits,
and a rewrite that uses it is refused as before.

## Every row is a §2.1 decision

"Widening this set is how a fabrication becomes permissible" (`SourceCorpus`).
A row belongs here only when using the tool *is* doing the practice, with
nothing further implied. `React` is not "frontend", `Docker` is not
"Kubernetes", and `scikit-learn` is not "MLOps".

The row is pytest. The owner asked for it on 2026-10-13, by name, and the
same day had the twelve other test runners that first shipped beside it taken
out: they were there because they are the same kind of thing, and that is the
argument `aliases.py` refuses. Each would be its own decision.

One thing a second row would meet. The fabrication guard reads "automated",
"testing" and "automation" as ordinary English, which is why this row needs
nothing from it. A practice written in words it does not know would be
refused by it as an unsupported noun, whatever this table says.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from packages.tailor.bullets import is_rewritable
from packages.tailor.guard import _TOKEN_RE, normalize


@dataclass(frozen=True)
class Practice:
    """One practice, how a posting writes it, its tool, and what the tool does."""

    #: Lower-case, as `normalize` leaves each word. Every form is the same
    #: practice written another way.
    forms: frozenset[str]
    #: One word each, lower-case: what a bullet has to name.
    tools: frozenset[str]
    #: Words for what the tool is used for. One has to stand near the tool
    #: for the bullet to have said the practice was done.
    uses: frozenset[str]


PRACTICES: tuple[Practice, ...] = (
    Practice(
        forms=frozenset({"automated testing", "automated tests", "test automation"}),
        tools=frozenset({"pytest"}),
        uses=frozenset(
            {
                "test",
                "tests",
                "tested",
                "testing",
                "check",
                "checks",
                "checked",
                "checking",
                "suite",
                "suites",
                "coverage",
                "assertions",
            }
        ),
    ),
)

_BY_FORM: dict[str, Practice] = {
    form: practice for practice in PRACTICES for form in practice.forms
}

#: How many words either side of the tool a word for its use may stand:
#: "tested every warehouse load with pytest" is five.
_NEAR = 5
#: Fewer words than this between two commas is an item in a list, however the
#: line opens: "Testing: pytest, Playwright" names two tools and reports
#: nothing.
_STATEMENT_WORDS = 4
#: How many words either side of the tool, or before the practice, a denial
#: is read as being about it.
_DENIAL_REACH = 4
#: Words between the practice and its tool in a revision, at most: "automated
#: testing via pytest", "pytest checks for automated testing".
_BESIDE = 3
#: And what those words may be: a preposition or an article, or the word for
#: the tool's use. "Automated testing and ran pytest checks" is two claims.
_JOINING: frozenset[str] = frozenset(
    {"via", "in", "with", "using", "through", "by", "of", "for", "as", "the", "a", "an", "its"}
)
#: A label in front of a list: "Testing: " or "Testing  " before the items.
_LABEL = re.compile(r"^\s*[^,:]{1,40}?(?::\s+|\s{2,})")

_DENIALS: frozenset[str] = frozenset(
    {"not", "no", "never", "without", "neither", "nor", "instead", "rather", "didnt"}
    | {"dont", "doesnt", "wasnt", "werent", "isnt", "cannot", "cant", "wont", "lacked"}
)
#: Directly before the tool, these say it went away.
_REMOVALS: frozenset[str] = frozenset({"replaced", "removed", "dropped", "retired", "deprecated"})
#: Anywhere before the tool, these make the line a plan and not a record.
_PLANS: frozenset[str] = frozenset(
    {"plan", "plans", "planned", "planning", "learn", "learning", "study", "studying"}
    | {"hope", "hopes", "hoping", "intend", "intends", "intending", "want", "wants", "wanting"}
    | {"aim", "aims", "aiming", "will", "would", "going", "future", "eventually", "soon"}
    | {"exploring", "considering", "interested", "aspire", "aspiring", "wish"}
)

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9'\u2019+#-]*")


def _line_words(text: str) -> list[tuple[str, str, int]]:
    """Each word of `text`: folded for comparison, as written, and where it starts."""
    found: list[tuple[str, str, int]] = []
    for match in _WORD.finditer(text):
        raw = match.group(0)
        folded = raw.lower().replace("'", "").replace("\u2019", "").strip("-")
        found.append((folded, raw, match.start()))
    return found


def phrase(term: str) -> str:
    """`term` as the table is keyed: each word normalized, single spaces."""
    return " ".join(word for word in (normalize(raw) for raw in _TOKEN_RE.findall(term)) if word)


def practice_for(term: str) -> Practice | None:
    """The practice `term` names, or None when it names none in the table."""
    return _BY_FORM.get(phrase(term))


def phrases() -> tuple[str, ...]:
    """Every form in the table, longest first, for finding one in a posting."""
    return tuple(sorted(_BY_FORM, key=len, reverse=True))


def _in_a_statement(line: str, tool_start: int) -> bool:
    """Whether the tool at `tool_start` stands in a sentence and not in a list.

    The bullet classifier reads "Testing: pytest, Playwright" as a bullet,
    because it opens like one. Between its commas there is one word.
    """
    label = _LABEL.match(line)
    begin = label.end() if label is not None and label.end() <= tool_start else 0
    before = line.rfind(", ", begin, tool_start)
    after = line.find(", ", tool_start)
    piece = line[(before + 2 if before != -1 else begin) : (after if after != -1 else len(line))]
    return len(_WORD.findall(piece)) >= _STATEMENT_WORDS


def affirmed_in(term: str, line: str) -> str | None:
    """The tool, as written, when `line` says it was used for `term`'s practice.

    None for a line that only names the tool, lists it, plans it or denies
    it. See the module's *What counts as evidence*.
    """
    practice = practice_for(term)
    if practice is None or not is_rewritable(line):
        return None
    words = _line_words(line)
    folded = [word for word, _, _ in words]
    for index, (word, written, start) in enumerate(words):
        if word not in practice.tools or not _in_a_statement(line, start):
            continue
        near = folded[max(0, index - _NEAR) : index] + folded[index + 1 : index + 1 + _NEAR]
        if not practice.uses & set(near):
            continue
        around = (
            folded[max(0, index - _DENIAL_REACH) : index]
            + folded[index + 1 : index + 1 + _DENIAL_REACH]
        )
        if _DENIALS & set(around):
            continue
        if index and folded[index - 1] in _REMOVALS:
            continue
        if _PLANS & set(folded[:index]):
            continue
        return written
    return None


def stands_beside(term: str, tool: str, text: str) -> bool:
    """Whether `text` writes the practice next to its tool, and does not deny it.

    The practice is true of what the tool was used for and of nothing else in
    the bullet. A revision that puts it three clauses away, or after "not",
    has kept every word and changed what is claimed.
    """
    practice = practice_for(term)
    joining = _JOINING | (practice.uses if practice is not None else frozenset())
    wanted = phrase(term).split()
    folded = [word for word, _, _ in _line_words(text)]
    name = tool.lower()
    spans = [
        (start, start + len(wanted))
        for start in range(len(folded) - len(wanted) + 1)
        if folded[start : start + len(wanted)] == wanted
    ]
    places = [index for index, word in enumerate(folded) if word == name]
    for start, end in spans:
        if _DENIALS & set(folded[max(0, start - _DENIAL_REACH) : start]):
            continue
        for place in places:
            between = folded[end:place] if place >= end else folded[place + 1 : start]
            if len(between) <= _BESIDE and set(between) <= joining:
                return True
    return False


__all__ = [
    "PRACTICES",
    "Practice",
    "affirmed_in",
    "phrase",
    "phrases",
    "practice_for",
    "stands_beside",
]
