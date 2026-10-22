"""Revise only the bullets that can truthfully gain a term the posting asks for.

The ordinary tailorer sends every bullet to a model with the whole posting
and asks for a rewrite. Run on the owner's résumé for one posting on
2026-10-13, that changed eight bullets and gained nothing: keyword coverage
stayed at 15 of 36, the recruiter score fell, and what the rewrites did add
was the posting's furniture ("application", "data systems"). Coverage could
not have moved. The scorer counts a term the résumé backs under any alias,
and the guard refuses a term it does not back, so a rewrite has nothing to
win and everything in the bullet to lose.

This asks the question first and the model second. The owner's fork of
`ai-job-search` sorts a posting's keywords four ways against the CV
(`.claude/commands/apply.md`, step 5d), and a model does the sorting there.
Here it is computed, from the same term list `ats.score` counts:

- **covered**: the résumé says it.
- **supported synonym**: the résumé says it another way, by the alias table
  (`ML` for "machine learning"). An ATS that matches words does not see it.
- **supported, not stated**: no form of it is on the résumé, and a source
  that may speak for the owner supports it. A bullet that says pytest ran
  its checks supports "automated testing" (`practices.py`, which is one row
  and says what counts as saying so), and a GitHub project supports what
  GitHub reports of it (`evidence.py`).
- **gap**: none of those. Never written in, by anyone.

Only the second and third are opportunities, and only where one bullet holds
the evidence. Then:

- **Only those bullets reach a model**, each with its own target terms and
  the evidence for each. Not the posting. A résumé with no opportunity makes
  no call at all.
- **A targeted revision adds its terms, and that is all it does.**
  `rewrite.vet` holds it to everything a rewrite is held to. `taken_away`
  holds it to more: every word of the bullet that is not grammar is still
  there, and numbers, units, names, model names and dates are words.
  `brought_in` is the other half: no word arrives but the terms asked for,
  since "across the company" is ordinary English and the guard lets ordinary
  English through.
- **A term goes where its evidence is.** A term is a target of the one
  bullet that holds its evidence and off limits to every other, so another
  project's tool never becomes a claim on an employer's bullet. A term
  supported only by the skills list, or by a GitHub project, is recorded
  with its source and no bullet is revised for it: the Projects section is
  where a project's facts are printed (§2.1).
- **One more try, once.** A revision the guard refuses is asked for again
  with the reason, and after that the bullet is the résumé's own.

Nothing here publishes, attaches or selects. `revise` returns a
`TailorResult` of the ordinary shape, one entry per bullet, so
`publish.publish_tailored` renders it like any other; `verify.py` reads the
PDF that comes out.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

import structlog

from packages.core.models import Project
from packages.llm.prompts import TAILOR_TARGETED_SYSTEM
from packages.llm.provider import LLMProvider
from packages.llm.router import temperature_for
from packages.tailor import ats, practices
from packages.tailor.aliases import equivalents
from packages.tailor.bullets import tailorable_bullets
from packages.tailor.evidence import project_source_text
from packages.tailor.guard import (
    _ATTRIBUTED_SECTIONS,
    _TOKEN_RE,
    SourceCorpus,
    _index,
    normalize,
    singular,
    stem,
)
from packages.tailor.parse import ParsedResume
from packages.tailor.rewrite import BulletRewrite, TailorResult, _clean, is_substantive, vet
from packages.tailor.technologies import _separated

log = structlog.get_logger(__name__)

SYSTEM_PROMPT = TAILOR_TARGETED_SYSTEM.text

#: What the report says when there is nothing a model could truthfully add.
NO_GAIN = "no supported keyword gain available"

#: Terms one bullet is revised for at most. Three in a sentence is a list of
#: keywords with a verb in front of it.
MAX_TARGETS_PER_BULLET = 2

#: The first ask and one repair. Not a loop.
MAX_ATTEMPTS = 2

#: Grammar. A targeted revision may move or change these; every other word of
#: the bullet has to survive it.
_FUNCTION_WORDS_TEXT = """
a an the and or of in on to for by with from at as into onto across over
via using through that which while plus per is are was were be it its
"""

_FUNCTION_WORDS: frozenset[str] = frozenset(_FUNCTION_WORDS_TEXT.split())


class Status(StrEnum):
    COVERED = "covered"
    SYNONYM = "supported synonym"
    SUPPORTED = "supported, not stated"
    GAP = "gap"


class Said:
    """What a piece of text says, word for word. No alias is read into it."""

    def __init__(self, text: str) -> None:
        words = _words(text)
        self._words = {form for word in words for form in (word, singular(word), stem(word))}
        self._joined = " " + " ".join(singular(word) for word in words) + " "

    def says(self, term: str) -> bool:
        words = _words(term)
        if not words:
            return False
        if len(words) == 1:
            word = words[0]
            return bool({word, singular(word), stem(word)} & self._words)
        return " " + " ".join(singular(word) for word in words) + " " in self._joined


def _words(text: str) -> list[str]:
    """`text` as normalized words, a compound split into its parts."""
    return [
        word for word in (normalize(raw) for raw in _TOKEN_RE.findall(_separated(text))) if word
    ]


@dataclass(frozen=True)
class Term:
    """One term the posting asks for, and how the résumé stands against it."""

    term: str
    status: Status
    #: Where the support is: a corpus reference (`experience:1`), `shared` for
    #: the skills list and the other unattributed sections, `github:<name>`.
    sources: tuple[str, ...] = ()
    #: What supports it there: the alias the résumé wrote, the tool a bullet
    #: names, the project. None when covered or a gap.
    via: str | None = None
    #: The tailorable bullet that can be revised to state it, or None.
    bullet: int | None = None


@dataclass(frozen=True)
class Target:
    """One term to add to one bullet, and what in the bullet supports it."""

    term: str
    evidence: str
    #: True when the evidence is a tool and the term its practice
    #: (`practices.py`); False when the term is the evidence written out.
    practice: bool = False


@dataclass(frozen=True)
class Plan:
    """A posting's terms sorted against one résumé, and what can be done."""

    section: str | None
    bullets: tuple[str, ...]
    terms: tuple[Term, ...]
    #: Bullet index to the terms it will be revised for.
    targets: Mapping[int, tuple[Target, ...]] = field(default_factory=dict)

    def having(self, status: Status) -> tuple[Term, ...]:
        return tuple(term for term in self.terms if term.status is status)

    @property
    def gaps(self) -> tuple[str, ...]:
        """Terms nothing supports. Never written in, by anyone."""
        return tuple(term.term for term in self.having(Status.GAP))

    @property
    def calls_needed(self) -> int:
        """Model calls at most: one per targeted bullet, and one repair each."""
        return len(self.targets) * MAX_ATTEMPTS


def posting_terms(job_description: str) -> list[str]:
    """The terms `ats.score` counts, with a practice read as one term.

    `job_terms` emits a multi-word skill whole only when the alias table
    knows it. "Automated testing" is in `practices` and not there, so it
    arrives as "automated" and "testing": two gaps, and two words a revision
    would be refused for using.
    """
    said = practices.phrase(ats.requirements_text(job_description))
    whole: list[str] = []
    claimed: list[practices.Practice] = []
    for form in practices.phrases():
        practice = practices.practice_for(form)
        if practice is None or practice in claimed or f" {form} " not in f" {said} ":
            continue
        claimed.append(practice)
        whole.append(form)

    used = {form for phrase in whole for word in phrase.split() for form in (word, singular(word))}
    rest = [
        term
        for term in ats.requirement_terms(job_description)
        if practices.phrase(term) not in whole and not (set(_words(term)) & used)
    ]
    return whole + rest


def _document(parsed: ParsedResume) -> str:
    """The résumé as it will be printed: its sections, not `raw_lines`.

    `apply_rewrites` leaves `raw_lines` as the source wrote them on purpose,
    so a tailored résumé's own text is in its sections.
    """
    lines = [*parsed.preamble]
    for section in parsed.sections.values():
        lines.extend(section)
    return "\n".join(lines)


def _shared(parsed: ParsedResume) -> str:
    lines = [*parsed.preamble]
    for name, section in parsed.sections.items():
        if name not in _ATTRIBUTED_SECTIONS:
            lines.extend(section)
    return "\n".join(lines)


def _as_written(form: str, bullet: str) -> str:
    """`form` the way `bullet` spelled it (`ML` for `ml`)."""
    for raw in _TOKEN_RE.findall(_separated(bullet)):
        if normalize(raw) == form or singular(normalize(raw)) == form:
            return str(raw).strip(".,;:")
    return form


#: An acronym as a bullet writes it: capitals, with a plural "s" ("APIs").
_ACRONYM = re.compile(r"[A-Z]{2,6}s?")


def _abbreviates(acronym: str, term: str) -> bool:
    """Whether `acronym`'s letters begin `term`'s words, in order (`UI`, user interface)."""
    letters = acronym.removesuffix("s")
    words = term.replace("-", " ").split()
    return len(words) == len(letters) and all(
        word[0].lower() == letter.lower() for word, letter in zip(words, letters, strict=True)
    )


def _in_number_of(acronym: str, term: str) -> str:
    """`term` singular when the bullet's acronym is: "an LLM" is one model."""
    if acronym.endswith("s"):
        return term
    *head, last = term.split()
    return " ".join([*head, singular(last)])


def expand_acronyms(bullet: str, targets: Sequence[Target]) -> str | None:
    """`bullet` with each target written out beside its acronym, or None.

    A target qualifies when its evidence is an acronym this bullet uses and
    the acronym's letters begin the term's words: `Streamlit UI` becomes
    `Streamlit UI (user interface)`. The bullet already states that fact;
    this only spells it, so no model is needed, and a small local one asked
    to do it added words a targeted revision may not (2026-10-21).

    All or nothing, so a bullet is never half written by code and half by a
    model. None for an alias that is not an abbreviation ("ETL" for "data
    pipelines"), an acronym inside another word ("GUI"), one already followed
    by brackets, or a practice. The caller still holds the result to `judge`.
    """
    candidate = bullet
    for target in targets:
        acronym = target.evidence
        if (
            target.practice
            or not _ACRONYM.fullmatch(acronym)
            or not _abbreviates(acronym, target.term)
        ):
            return None
        found = re.search(rf"(?<![\w-]){re.escape(acronym)}(?![\w-])(?!\s*\()", candidate)
        if found is None:
            return None
        spelled = f" ({_in_number_of(acronym, target.term)})"
        candidate = candidate[: found.end()] + spelled + candidate[found.end() :]
    return candidate


def classify(
    parsed: ParsedResume, job_description: str, *, projects: Sequence[Project] = ()
) -> Plan:
    """Sort a posting's terms against a résumé. No model is asked anything."""
    corpus = SourceCorpus.from_resume(parsed)
    section, bullets = tailorable_bullets(parsed)
    whole = Said(_document(parsed))
    shared_text = _shared(parsed)
    places = [(item.ref, item.text) for item in corpus.items] + [("shared", shared_text)]
    said_at = [(ref, Said(text)) for ref, text in places]
    said_by = [Said(bullet) for bullet in bullets]
    github = [(project, Said(project_source_text(project))) for project in projects]

    room: dict[int, list[Target]] = {}

    def claim(candidates: list[tuple[int, str]], term: str, *, practice: bool) -> int | None:
        for index, evidence in candidates:
            taken = room.setdefault(index, [])
            if len(taken) < MAX_TARGETS_PER_BULLET:
                taken.append(Target(term=term, evidence=evidence, practice=practice))
                return index
        return None

    terms: list[Term] = []
    for term in posting_terms(job_description):
        if whole.says(term):
            sources = tuple(ref for ref, said in said_at if said.says(term))
            terms.append(Term(term, Status.COVERED, sources))
            continue

        key = " ".join(term.lower().split())
        alias = next((form for form in sorted(equivalents(key) - {key}) if whole.says(form)), None)
        if alias is not None:
            holding = [
                (index, _as_written(alias, bullet))
                for index, bullet in enumerate(bullets)
                if said_by[index].says(alias)
            ]
            terms.append(
                Term(
                    term,
                    Status.SYNONYM,
                    sources=tuple(ref for ref, said in said_at if said.says(alias)),
                    via=alias,
                    bullet=claim(holding, term, practice=False),
                )
            )
            continue

        if practices.practice_for(term) is not None:
            # A statement in a bullet, line by line. The entry's title and its
            # stack line name the tool and say nothing was done with it, and
            # the skills list is not read at all.
            stated = [
                (ref, tool)
                for ref, text in ((item.ref, item.text) for item in corpus.items)
                for line in text.split("\n")
                if (tool := practices.affirmed_in(term, line)) is not None
            ]
            if stated:
                naming = [
                    (index, tool)
                    for index, bullet in enumerate(bullets)
                    if (tool := practices.affirmed_in(term, bullet)) is not None
                ]
                terms.append(
                    Term(
                        term,
                        Status.SUPPORTED,
                        sources=tuple(dict.fromkeys(ref for ref, _ in stated)),
                        via=stated[0][1],
                        bullet=claim(naming, term, practice=True),
                    )
                )
                continue

        # Said, word for word or by an alias. `keywords._supported` also takes a
        # substring, and "how" is inside "show": a project's description
        # backed a term it never used.
        forms = equivalents(key) or {key}
        project = next(
            (p for p, says in github if any(says.says(form) for form in sorted(forms))), None
        )
        if project is not None:
            # A project's facts are printed in the Projects section, under the
            # project. No bullet is revised for them.
            terms.append(
                Term(term, Status.SUPPORTED, sources=(f"github:{project.name}",), via=project.name)
            )
            continue

        terms.append(Term(term, Status.GAP))

    return Plan(
        section=section,
        bullets=tuple(bullets),
        terms=tuple(terms),
        targets={index: tuple(found) for index, found in sorted(room.items()) if found},
    )


def taken_away(original: str, candidate: str) -> tuple[str, ...]:
    """Words of `original` that `candidate` no longer has, as written.

    Read through the guard's own index, so a short form written out is not a
    word lost: a revision that says "machine learning" still has `ML`.
    """
    have = _index(_separated(candidate))
    have |= {stem(word) for word in have}
    gone: list[str] = []
    for raw in _TOKEN_RE.findall(_separated(original)):
        word = normalize(raw)
        if not word or word in _FUNCTION_WORDS:
            continue
        if {word, singular(word), stem(word)} & have:
            continue
        shown = raw.strip(".,;:")
        if shown not in gone:
            gone.append(shown)
    return tuple(gone)


def brought_in(original: str, candidate: str, targets: Sequence[Target]) -> tuple[str, ...]:
    """Words `candidate` has that are neither the bullet's nor a target's.

    The other half of `taken_away`. The fabrication guard lets ordinary
    English through, and ordinary English is enough to enlarge a claim:
    "across the company" is three words it has no quarrel with. A targeted
    revision was asked for its terms and nothing else.
    """
    had = _index(_separated(original))
    had |= {stem(word) for word in had}
    asked = {
        form
        for target in targets
        for word in _words(f"{target.term} {target.evidence}")
        for form in (word, singular(word), stem(word))
    }
    new: list[str] = []
    for raw in _TOKEN_RE.findall(_separated(candidate)):
        word = normalize(raw)
        if not word or word in _FUNCTION_WORDS:
            continue
        if {word, singular(word), stem(word)} & (had | asked):
            continue
        shown = str(raw).strip(".,;:")
        if shown not in new:
            new.append(shown)
    return tuple(new)


@dataclass(frozen=True)
class Outcome:
    """What happened to one bullet that was sent for revision."""

    bullet: int
    targets: tuple[Target, ...]
    #: The target terms the kept revision states. Empty when none was kept.
    added: tuple[str, ...] = ()
    attempts: int = 0
    #: One per attempt that was not used, in order. Résumé words can be in
    #: these: they are for the owner's report and are never logged.
    refusals: tuple[str, ...] = ()


@dataclass(frozen=True)
class Revision:
    """A targeted pass over one résumé: the plan, and what came of it."""

    plan: Plan
    #: One entry per tailorable bullet, in order, as `publish` expects. A
    #: bullet that was not sent, or whose revision was refused, is the
    #: résumé's own.
    result: TailorResult
    outcomes: tuple[Outcome, ...] = ()
    calls: int = 0

    @property
    def gained(self) -> tuple[str, ...]:
        return tuple(term for outcome in self.outcomes for term in outcome.added)

    @property
    def message(self) -> str:
        if not self.plan.targets:
            return NO_GAIN
        asked = sum(len(outcome.targets) for outcome in self.outcomes)
        return f"{len(self.gained)} of {asked} supported terms added, in {self.calls} model calls"


def _prompt(bullet: str, targets: Sequence[Target], refused: str | None) -> str:
    parts = [
        f"Original bullet:\n{bullet.strip()}",
        "Target terms, and the evidence in this bullet for each:\n"
        + "\n".join(f"- {target.term} (evidence: {target.evidence})" for target in targets),
    ]
    if refused is not None:
        parts.append(
            f"Your last revision could not be used: {refused}\n"
            "Keep every word of the original and add only the target terms."
        )
    parts.append("Revised bullet:")
    return "\n\n".join(parts)


def judge(
    bullet: str,
    candidate: str,
    targets: Sequence[Target],
    corpus: SourceCorpus,
    off_limits: tuple[str, ...],
) -> tuple[tuple[str, ...], str | None, tuple[str, ...], int]:
    """The terms a revision may be kept for, or why it may not be kept.

    `off_limits` is every term of the posting this bullet was not sent for.
    A gap is one, and so is a term the résumé supports somewhere else: the
    revision of an employer's bullet may not pick up what a project's bullet
    is evidence of.

    Returns the added terms, the reason when there are none, the figures the
    guard found dropped, and how many claims it traced.
    """
    accepted, reason, report = vet(bullet, candidate, corpus, off_limits)
    if not accepted:
        return (), reason, report.dropped_figures, report.checked

    gone = taken_away(bullet, candidate)
    if gone:
        named = ", ".join(repr(word) for word in gone)
        return (), f"takes {named} out of the bullet; a targeted revision only adds", (), 0

    extra = brought_in(bullet, candidate, targets)
    if extra:
        named = ", ".join(repr(word) for word in extra)
        return (), f"adds {named}; a targeted revision adds its terms and nothing else", (), 0

    said = Said(candidate)
    added = tuple(target.term for target in targets if said.says(target.term))
    if not added or not is_substantive(bullet, candidate):
        return (), "adds none of the terms it was revised for", (), report.checked

    # A practice keeps the scope its evidence had. By now every word of the
    # bullet is still there and none has arrived but the terms asked for, and
    # the term can still have been put somewhere else in the sentence: after
    # an "and" it is a second claim, not a name for what the tool did.
    for target in targets:
        stated = target.practice and target.term in added
        if stated and not practices.stands_beside(target.term, target.evidence, candidate):
            return (
                (),
                f"does not put {target.term!r} beside {target.evidence}; "
                "the term is true of what the tool did and of nothing else",
                (),
                report.checked,
            )
    return added, None, (), report.checked


async def _revise_bullet(
    provider: LLMProvider,
    index: int,
    bullet: str,
    targets: tuple[Target, ...],
    corpus: SourceCorpus,
    off_limits: tuple[str, ...],
) -> tuple[BulletRewrite, Outcome]:
    refusals: list[str] = []
    dropped: tuple[str, ...] = ()
    failed = False
    attempts = 0

    for _ in range(MAX_ATTEMPTS):
        attempts += 1
        try:
            raw = await provider.complete(
                SYSTEM_PROMPT,
                _prompt(bullet, targets, refusals[-1] if refusals and not failed else None),
                max_tokens=300,
                temperature=temperature_for("tailor_resume"),
            )
        except Exception as exc:  # noqa: BLE001 - a provider failure is a fallback
            log.warning("targeted_provider_failed", error=type(exc).__name__, attempt=attempts)
            refusals.append(f"provider error: {type(exc).__name__}")
            failed = True
            continue

        failed = False
        candidate = _clean(raw, bullet)
        added, reason, dropped, checked = judge(bullet, candidate, targets, corpus, off_limits)
        if added:
            return (
                BulletRewrite(
                    original=bullet, tailored=candidate, changed=True, entities_checked=checked
                ),
                Outcome(index, targets, added, attempts, tuple(refusals)),
            )
        # The reason can name the bullet's own words. It stays on the result;
        # the log gets which attempt it was and nothing of the résumé (§10).
        log.info("targeted_refused", attempt=attempts, figures=len(dropped))
        refusals.append(reason or "not usable")

    return (
        BulletRewrite(
            original=bullet,
            tailored=bullet,
            rejected_reason=refusals[-1],
            provider_failed=failed,
            dropped_figures=list(dropped),
        ),
        Outcome(index, targets, (), attempts, tuple(refusals)),
    )


def _spelled_out(
    index: int,
    bullet: str,
    targets: tuple[Target, ...],
    corpus: SourceCorpus,
    off_limits: tuple[str, ...],
) -> tuple[BulletRewrite, Outcome] | None:
    """The bullet with its acronyms written out, if `judge` keeps it. No model call.

    None sends the bullet to the model as before: nothing to expand, or an
    expansion the guard would refuse from a model too.
    """
    candidate = expand_acronyms(bullet, targets)
    if candidate is None:
        return None
    added, _, _, checked = judge(bullet, candidate, targets, corpus, off_limits)
    if set(added) != {target.term for target in targets}:
        return None
    return (
        BulletRewrite(original=bullet, tailored=candidate, changed=True, entities_checked=checked),
        Outcome(index, targets, added, attempts=0),
    )


async def revise(
    provider: LLMProvider,
    parsed: ParsedResume,
    job_description: str,
    *,
    projects: Sequence[Project] = (),
    plan: Plan | None = None,
) -> Revision:
    """Revise the bullets that have something to gain. The rest are untouched.

    With no opportunity the provider is never called, and the result is the
    résumé as it stands with `Revision.message` saying why.
    """
    plan = plan or classify(parsed, job_description, projects=projects)
    corpus = SourceCorpus.from_resume(parsed)

    rewrites: list[BulletRewrite] = []
    outcomes: list[Outcome] = []
    for index, bullet in enumerate(plan.bullets):
        targets = plan.targets.get(index)
        if not targets:
            rewrites.append(BulletRewrite(original=bullet, tailored=bullet))
            continue
        asked = {target.term for target in targets}
        off_limits = tuple(term.term for term in plan.terms if term.term not in asked)
        spelled = _spelled_out(index, bullet, targets, corpus, off_limits)
        if spelled is not None:
            rewrite, outcome = spelled
        else:
            rewrite, outcome = await _revise_bullet(
                provider, index, bullet, targets, corpus, off_limits
            )
        rewrites.append(rewrite)
        outcomes.append(outcome)

    return Revision(
        plan=plan,
        result=TailorResult(
            bullets=rewrites,
            rejected=sum(1 for rewrite in rewrites if rewrite.guard_refused),
            provider_failures=sum(1 for rewrite in rewrites if rewrite.provider_failed),
        ),
        outcomes=tuple(outcomes),
        calls=sum(outcome.attempts for outcome in outcomes),
    )


__all__ = [
    "MAX_ATTEMPTS",
    "MAX_TARGETS_PER_BULLET",
    "NO_GAIN",
    "Outcome",
    "Plan",
    "Revision",
    "Said",
    "Status",
    "Target",
    "Term",
    "brought_in",
    "classify",
    "expand_acronyms",
    "judge",
    "posting_terms",
    "revise",
    "taken_away",
]
