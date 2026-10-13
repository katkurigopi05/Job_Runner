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
is. So a practice may be written **beside its tool**, and nowhere else. This
module is only the table; `targeted.py` is what holds each of these:

- **One way.** A tool is evidence of its practice. A practice is evidence of
  nothing.
- **In the bullet that names the tool.** The term is a target of that bullet
  and off limits to every other, so a side project's pytest never puts
  "automated testing" on an employer's bullet. The skills list names a tool
  and is no bullet: the term is recorded as supported and nothing is revised.
- **With the tool still there.** A targeted revision may take no word out of
  the bullet (`targeted.taken_away`), so the practice is never left standing
  on its own.
- **Only when asked.** The ordinary rewrite does not read this table. There
  the term is one the résumé does not state, it goes to the model as off
  limits, and a rewrite that uses it is refused as before.

## Every row is a §2.1 decision

"Widening this set is how a fabrication becomes permissible" (`SourceCorpus`).
A row belongs here only when using the tool *is* doing the practice, with
nothing further implied. `pytest` is automated testing. `React` is not
"frontend", `Docker` is not "Kubernetes", and `scikit-learn` is not "MLOps".

The owner asked for the first row on 2026-10-13, by name, in the test that
holds it (`tests/test_targeted_tailoring.py`). Nothing else was added.

One thing a second row would meet. The fabrication guard reads "automated",
"testing" and "automation" as ordinary English, which is why this row needs
nothing from it. A practice written in words it does not know would be
refused by it as an unsupported noun, whatever this table says.
"""

from __future__ import annotations

from dataclasses import dataclass

from packages.tailor.guard import _TOKEN_RE, normalize


@dataclass(frozen=True)
class Practice:
    """One practice, the ways a posting writes it, and the tools that are it."""

    #: Lower-case, as `normalize` leaves each word. Every form is the same
    #: practice written another way.
    forms: frozenset[str]
    #: One word each, lower-case: what a bullet has to name.
    tools: frozenset[str]


PRACTICES: tuple[Practice, ...] = (
    Practice(
        forms=frozenset({"automated testing", "automated tests", "test automation"}),
        tools=frozenset(
            {
                "pytest",
                "unittest",
                "junit",
                "testng",
                "nunit",
                "xunit",
                "jest",
                "vitest",
                "mocha",
                "rspec",
                "playwright",
                "cypress",
                "selenium",
            }
        ),
    ),
)

_BY_FORM: dict[str, Practice] = {
    form: practice for practice in PRACTICES for form in practice.forms
}


def phrase(term: str) -> str:
    """`term` as the table is keyed: each word normalized, single spaces."""
    return " ".join(word for word in (normalize(raw) for raw in _TOKEN_RE.findall(term)) if word)


def practice_for(term: str) -> Practice | None:
    """The practice `term` names, or None when it names none in the table."""
    return _BY_FORM.get(phrase(term))


def phrases() -> tuple[str, ...]:
    """Every form in the table, longest first, for finding one in a posting."""
    return tuple(sorted(_BY_FORM, key=len, reverse=True))


def evidence_in(term: str, text: str) -> str | None:
    """The tool in `text` that is evidence of `term`, as `text` wrote it."""
    practice = practice_for(term)
    if practice is None:
        return None
    for match in _TOKEN_RE.finditer(text):
        raw = match.group(0).strip(".,;:()")
        if normalize(raw) in practice.tools:
            return raw
    return None


__all__ = [
    "PRACTICES",
    "Practice",
    "evidence_in",
    "phrase",
    "phrases",
    "practice_for",
]
