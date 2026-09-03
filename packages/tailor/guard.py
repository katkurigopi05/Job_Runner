"""The fabrication guard — CLAUDE.md §2.1, and the merge gate for tailoring.

Rewriting may rephrase, reorder, re-emphasize, and inject keywords *already
supported by the source*. It may not add a skill, employer, date, credential,
or metric that is not there. This module is what makes that checkable rather
than aspirational.

## How it decides

Extract from the candidate output every token that could carry a factual
claim, then require each to trace back to the source corpus:

- **Numbers** — the metric case. "improved latency by 40%" is a fabrication
  unless 40 appears in the source.
- **Proper nouns** — employers, products, technologies. A capitalized token
  that is not ordinary English has to come from somewhere.
- **Acronyms** — skills and credentials. AWS, PMP, CISSP.
- **Years** — the date case.

Ordinary English words, however rearranged, are free. That is exactly the
latitude §2.1 grants: rephrasing is allowed, inventing is not.

The word list started out sized for résumé bullets, which are terse and
third-person. A cover letter is first-person prose, so "My", "We" and "Please"
arrive capitalized at the start of sentences and were being read as proper
nouns — enough to reject nearly every letter for saying "My". Those words
carry no factual claim in any position, so they belong here regardless of
which surface is being checked.

The last group arrived with noun-phrase extraction. A tagger sees "the closest
match for this role" as three nouns, and in a bullet that would be three
claims — but they are the furniture of English prose and assert nothing about
the applicant. "machine learning" and "message queue" are not on this list and
are still checked, which is the line: ordinary vocabulary is free, technical
vocabulary is a claim.

## Why it is deliberately strict

A guard that misses a fabrication puts a false claim on a real application in
the owner's name. A guard that is over-strict merely refuses a rewrite and
falls back to the source résumé, which is always safe. The asymmetry is the
whole design, and it is why the checks below reject on doubt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from packages.tailor.chunk import available as chunker_available
from packages.tailor.chunk import claim_words
from packages.tailor.parse import ParsedResume

# A number keeps its unit: 40TB, 800ms and 2M are single claims, not a
# number sitting next to an unrelated acronym.
_TOKEN_RE = re.compile(r"\d[\d,.]*%?[A-Za-z]*|[A-Za-z][A-Za-z0-9+#./-]*")
_NUMERIC_RE = re.compile(r"\d")
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")

#: Written numbers a rewrite may legitimately render as digits, and vice
#: versa. "three years" → "3 years" restates a source fact rather than
#: inventing one.
_NUMBER_WORDS: dict[str, str] = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
    "fifteen": "15",
    "twenty": "20",
    "thirty": "30",
    "forty": "40",
    "fifty": "50",
    "hundred": "100",
    "thousand": "1000",
    "million": "1000000",
}

#: Ordinary English. A capitalized token from this set is sentence casing,
#: not a proper noun, so it never needs to trace to the source.
#:
#: Kept as text rather than an inline literal so the formatter leaves it
#: readable — a wall of quoted strings is a list nobody will maintain.
_COMMON_WORDS_TEXT = """
a ability about above achieved across after again against all almost also
although always among an and any api apis application applications approach
architected architecture are as at automation backend be because been before
being below between both build built but by changes clients code
collaborated component components created cut data database databases day
decreased delivered deployment design designed developed development drove
during each either else enabled end engineer engineering engineers enough
ensured even ever every expanded experience feature features few first
following for from frontend further growth had has have he her here his how
however i if impact implemented improved in increased infrastructure
integration into introduced is it its just last latency later launched least
led less level library like logic maintained managed many may member
mentored metrics migrated migration model models module more most much must
never new next no not now number of often on once only opportunity optimized
or other our over own owned part partner partnered people per performance
performed pipeline pipelines planned platform practice process processes
produced product production project projects quality queries query rather
reduced refactored release released reliability removed replaced
requirements research response result results review reviews role same scale
scaled scope senior service services several she shipped simplified since so
software solution solutions solved some speed staff standards strategy
streamlined structure such support supported system systems team teams
technology test testing tests than that the their them then there these they
this through throughout thus time to today too tool tooling tools traffic
type under until up upon use used user users using value version very via
was week well were what when where whether which while who will with within
without work worked workflow working works would wrote year years yet you
your

my me we us those whose another still why though hire hiring company letter
dear sincerely regards please thank thanks

server servers host hosts node nodes

match closest background position way fit interest attention detail focus
side front back reason point place kind sort number amount level

automate automated automating consolidate consolidated consolidating
coach coached coaching coordinate coordinated coordinating debug debugged
debugging deploy deployed deploying diagnose diagnosed diagnosing document
documented documenting establish established establishing grew grow growing
handle handled handling instrument instrumented instrumenting integrate
integrated integrating monitor monitored monitoring negotiate negotiated
negotiating operate operated operating ran run running resolve resolved
resolving review reviewed rewrite rewrote rewriting standardize standardized
standardizing test tested validate validated validating
"""
# The block above is the same fix CLAUDE.md records for cover letters, where
# "My", "We" and "Please" were read as proper nouns and rejected nearly every
# letter. A résumé bullet opens with a verb, and the guard's proper-noun test is
# capitalization — so a rewrite that opened with any verb the source did not
# already use was refused as a fabricated name. "Deployed the pipeline" failed
# against a source that said "Wrote the pipeline", for the word "Deployed".
#
# These carry no factual claim in any position; the claim is the object, and
# the object is still checked — "Deployed Kubernetes" still has to trace
# Kubernetes. Verbs that *do* assert seniority stay out: see _SCOPE_CLAIMS.
#
# `server servers host hosts node nodes` close a gap in the same list rather
# than widening it. `service`, `services`, `system`, `systems`,
# `infrastructure`, `platform`, `database`, `databases`, `pipeline` and
# `pipelines` were already here on the argument that a generic category noun
# asserts nothing — the claim is *which* one, and that is still checked. The
# machine words were simply missing, which the noun-phrase extractor made
# visible: it refused "cluster administration on bare metal servers" as a
# rewrite of "cluster administration on bare metal", on the noun "servers",
# while the identical sentence written with "systems" passed.
#
# `cluster` and `machine` are deliberately *not* added. `cluster` is load-
# bearing in `packages/tailor/recombination.py`'s own test — "Kubernetes
# cluster administration" is the recombined claim it exists to catch — and
# `machine` is half of "machine learning", which the alias table handles as a
# phrase. Removing either from the checked set would cost a real check to buy
# a cosmetic one.

_COMMON_WORDS: frozenset[str] = frozenset(_COMMON_WORDS_TEXT.split())


class EntityKind(StrEnum):
    NUMBER = "number"
    PROPER_NOUN = "proper_noun"
    ACRONYM = "acronym"
    YEAR = "year"
    #: A noun or attached adjective inside a noun phrase. This is the kind §9
    #: Gate 3 actually asks for; the four above are the capitalization proxy
    #: that stood in for it, and could not see "machine learning".
    NOUN = "noun"
    SCOPE = "scope"


#: Words that assert seniority or ownership rather than describe work.
#:
#: Checked like any other claim, because "led the migration" against a source
#: saying "worked on the migration" is a fabrication — of scope rather than of
#: fact, and the kind a résumé rewrite is most tempted by.
#:
#: The distinction from ordinary verbs is whether the word alone claims rank.
#: "Deployed" says what was done; "oversaw" says who you were while doing it.
_SCOPE_CLAIMS_TEXT = """
architect architected chief direct directed director drive drove head
lead leader led manage managed manager own owned principal
scale scaled scalability senior spearhead spearheaded sr staff
oversee oversaw overseeing found founded founding
pioneer pioneered pioneering champion championed championing
"""

_SCOPE_CLAIMS: frozenset[str] = frozenset(_SCOPE_CLAIMS_TEXT.split())


@dataclass(frozen=True)
class Entity:
    """A token in the output that carries a factual claim."""

    text: str
    kind: EntityKind
    normalized: str


@dataclass(frozen=True)
class Violation:
    """An entity that does not trace back to the source."""

    entity: Entity
    reason: str

    def __str__(self) -> str:
        return f"{self.entity.kind.value} {self.entity.text!r}: {self.reason}"


@dataclass
class GuardReport:
    """The verdict on one piece of candidate output."""

    ok: bool
    violations: list[Violation] = field(default_factory=list)
    checked: int = 0
    #: Which extractor produced the entities: "noun-phrase" when the tagger
    #: was available, "capitalization" when it was not. A weaker check has to
    #: be visible — a guard that quietly loses one is worse than a guard that
    #: never had it, because nobody re-reads a green test.
    extractor: str = "capitalization"
    #: Which corpus item the claims were held against. None means the check
    #: was document-wide — correct for a cover letter, too permissive for a
    #: bullet, and recorded either way so the two are never confused.
    scope_ref: str | None = None

    def summary(self) -> str:
        against = "source" if self.scope_ref is None else self.scope_ref
        if self.ok:
            return f"clean — {self.checked} entities traced to {against}"
        listed = "; ".join(str(v) for v in self.violations[:5])
        more = "" if len(self.violations) <= 5 else f" (+{len(self.violations) - 5} more)"
        return f"{len(self.violations)} unsupported: {listed}{more}"


class FabricationError(Exception):
    """Candidate output contains claims the source does not support."""

    def __init__(self, report: GuardReport) -> None:
        self.report = report
        super().__init__(report.summary())


def _strip(token: str) -> str:
    return token.strip(".,;:!?()[]{}\"'")


def stem(word: str) -> str:
    """Fold common suffixes, so `reliability` and `reliable` compare equal.

    Deliberately naive, and narrower than it looks. Suffix stripping cannot
    reach irregular forms or heavy derivations, and several pairs that matter
    here do not fold together:

        scale / scalability   -> scale, scalable      no match
        scale / scaled        -> scale, scal          no match
        lead / led            -> lead, led            no match
        async / asynchronous  -> async, asynchronou   no match

    Every one of those is caught by the explicit `_SCOPE_CLAIMS` list instead,
    which is why that list enumerates variants rather than relying on this.

    This docstring previously named `scale vs scalability` as the example it
    handles. It does not, and the blocking that looked like proof of the
    stemmer working was the word list doing it. Do not assume a variant is
    covered because a stemmer exists — check that it is either listed or
    genuinely folds.
    """
    w = word.lower()
    if len(w) <= 3:
        return w

    # Order matters: longest suffixes first
    suffixes = [
        ("ibilities", "ible"),
        ("ability", "able"),
        ("ibility", "ible"),
        ("ational", "ate"),
        ("tional", "tion"),
        ("ations", "ate"),
        ("ation", "ate"),
        ("ement", ""),
        ("ments", ""),
        ("ment", ""),
        ("ness", ""),
        ("ings", ""),
        ("ing", ""),
        ("ities", "ity"),
        ("ity", ""),
        ("ied", "y"),
        ("ies", "y"),
        ("ed", ""),
        ("es", ""),
        ("s", ""),
        ("ly", ""),
    ]

    for suff, replacement in suffixes:
        if w.endswith(suff) and len(w) - len(suff) >= 3:
            return w[: -len(suff)] + replacement

    return w


def normalize(token: str) -> str:
    """Fold a token to the form the corpus is indexed under.

    Lowercase, punctuation-stripped, naive de-pluralized, number-words mapped
    to digits, and thousands separators removed so `2,000` matches `2000`.
    """
    text = _strip(token).lower()
    if not text:
        return ""

    if text in _NUMBER_WORDS:
        return _NUMBER_WORDS[text]

    # 2,000 -> 2000 ; 40% -> 40 ; 2m -> 2m (the unit carries meaning)
    if _NUMERIC_RE.search(text):
        text = text.replace(",", "").rstrip("%+")

    return text


def singular(text: str) -> str:
    """Naive de-pluralization, used only for *matching*.

    Applied as an alternate index rather than folded into `normalize`, so
    "Kubernetes" is never reported back to the owner as "kubernete".
    """
    if len(text) > 3 and text.endswith("s") and not text.endswith(("ss", "us", "is")):
        return text[:-1]
    return text


def _classify(token: str) -> EntityKind | None:
    """What kind of claim this token carries, or None if it carries none."""
    bare = _strip(token)
    if not bare:
        return None

    normalized_bare = normalize(bare)
    if normalized_bare in _SCOPE_CLAIMS or stem(normalized_bare) in {
        stem(w) for w in _SCOPE_CLAIMS
    }:
        return EntityKind.SCOPE

    if _YEAR_RE.match(bare):
        return EntityKind.YEAR

    if _NUMERIC_RE.search(bare):
        # A bare ordinal or list marker is not a metric.
        return EntityKind.NUMBER

    # A spelled-out number is the same claim as its digits. `normalize` has
    # mapped "nine" to "9" since this module was written, but the test above
    # looks for a digit in the *raw* token — so "nine" was never classified as
    # anything and was therefore never checked at all. A rewrite could say
    # "nine facilities" against a source reading "four" and pass the guard,
    # which is exactly the fabrication class §2.1 exists to stop.
    #
    # Found by the same defect surfacing in `santifer/career-ops`
    # (fix(verify-cv-facts): catch fabricated headcounts outside software).
    # Theirs was a missing noun; ours was a missing numeral form.
    if normalized_bare in _NUMBER_WORDS.values() and bare.isalpha():
        return EntityKind.NUMBER

    if len(bare) >= 2 and bare.isupper() and bare.isalpha():
        return EntityKind.ACRONYM

    if bare[0].isupper() and normalized_bare not in _COMMON_WORDS:
        return EntityKind.PROPER_NOUN

    return None


def extract_entities(text: str) -> list[Entity]:
    """Every claim-carrying token in `text`, in order, deduplicated.

    Two passes. The capitalization pass catches numbers, years, acronyms and
    proper nouns. The noun-phrase pass — when the tagger is installed —
    catches the lowercase claims the first cannot see, which is most technical
    vocabulary as people actually write it.
    """
    seen: set[tuple[str, EntityKind]] = set()
    entities: list[Entity] = []

    for match in _TOKEN_RE.finditer(text):
        token = match.group(0)
        kind = _classify(token)
        if kind is None:
            continue
        normalized = normalize(token)
        if not normalized:
            continue
        key = (normalized, kind)
        if key in seen:
            continue
        seen.add(key)
        entities.append(Entity(text=_strip(token), kind=kind, normalized=normalized))

    # Nouns and their adjectives. Ordinary English is still free — §2.1 allows
    # rephrasing, and "oversaw" for "maintained" is rephrasing.
    #
    # A term the first pass already claimed is skipped rather than emitted
    # twice: "AWS" is one claim whether it is read as an acronym or as a noun,
    # and counting it twice inflates `checked` and reports it twice.
    already = {entity.normalized for entity in entities}

    for word in claim_words(text):
        normalized = normalize(word)
        if not normalized or normalized in _COMMON_WORDS or normalized in already:
            continue
        key = (normalized, EntityKind.NOUN)
        if key in seen:
            continue
        seen.add(key)
        already.add(normalized)
        entities.append(Entity(text=word, kind=EntityKind.NOUN, normalized=normalized))

    return entities


@dataclass(frozen=True)
class CorpusItem:
    """One attributable unit of the source — a job entry, or a project.

    The unit matters. A metric belongs to the employer it was earned under,
    and moving it onto a different employer's bullet is a fabrication even
    though every word of it appears somewhere in the résumé.
    """

    ref: str
    text: str
    tokens: frozenset[str]


def _index(text: str) -> set[str]:
    """Every form a token in `text` should be findable under."""
    tokens: set[str] = set()
    for match in _TOKEN_RE.finditer(text):
        normalized = normalize(match.group(0))
        if not normalized:
            continue
        tokens.add(normalized)
        tokens.add(singular(normalized))
        # Index the digit form of a written number too, so a source saying
        # "three" supports an output saying "3".
        if normalized in _NUMBER_WORDS:
            tokens.add(_NUMBER_WORDS[normalized])

    # Equivalent spellings of the same fact — "Postgres" also findable as
    # "PostgreSQL", "K8s" as "Kubernetes". Indexed here rather than checked at
    # lookup time so that `keywords.analyze` and `supports()` cannot disagree:
    # one decides which of the posting's terms the model is invited to use, the
    # other decides whether the result is a fabrication, and a term ruled safe
    # by the first and refused by the second is worse than never offering it.
    #
    # This widens the corpus, which `SourceCorpus` warns is how a fabrication
    # becomes permissible — see `packages/tailor/aliases.py` for the two rules
    # that keep the table to true equivalences rather than related terms.
    from packages.tailor.aliases import expand_phrases, expand_tokens

    tokens |= expand_tokens(tokens)
    tokens |= expand_phrases(" ".join(normalize(m.group(0)) for m in _TOKEN_RE.finditer(text)))
    return tokens


#: A bullet continues the entry above it; anything else starts a new one.
_BULLET_RE = re.compile(r"^\s*[-\u2022*\u2023\u25e6\u2013\u2014]\s+")

#: Sections where a claim is *attributed* to something — an employer, a
#: project — and so must not borrow from a sibling. Every other section
#: describes the person as a whole and is shared.
_ATTRIBUTED_SECTIONS = ("experience", "projects")


@dataclass
class SourceCorpus:
    """Everything a rewrite is allowed to draw on.

    The source résumé plus any other *verified* material — GitHub projects are
    included because they come from the owner's own account rather than from a
    model. Widening this set is how a fabrication becomes permissible, so it
    should be done deliberately and never quietly.

    Two scopes live here, and the difference is the point:

    - `tokens` is everything, and answers "is this fact true of the owner?"
    - `items` are the attributable units, and answer the harder question,
      "is this fact true of *the thing this bullet is about*?"

    A check with no scope asks only the first. `vet()` in `rewrite.py` supplies
    the scope so a bullet is held to the second.
    """

    tokens: set[str] = field(default_factory=set)
    #: Normalized full text, for substring checks a token set would miss.
    text: str = ""
    #: Attributable units, in document order. Empty means unstructured input,
    #: in which case scoping degrades to the document and says so.
    items: tuple[CorpusItem, ...] = ()
    #: Tokens available to every item — contact details, the skills list,
    #: education. These describe the person, not one job.
    shared: frozenset[str] = frozenset()
    #: Technologies the résumé lists for itself — see
    #: `packages/tailor/technologies.py`. Unlike everything else here, this is
    #: not used to decide what a rewrite may *say*: it is what a rewrite may
    #: not silently drop. Empty for a flat corpus, which disables that check
    #: rather than guessing at an inventory from unstructured text.
    technologies: frozenset[str] = frozenset()

    @property
    def attributed(self) -> frozenset[str]:
        """Tokens that some entry claims as its own.

        A token here is not merely true of the owner; the résumé says *where*
        it is true. `supports` uses that to stop the skills list laundering a
        sibling's technology onto this entry — see its docstring.
        """
        claimed: set[str] = set()
        for item in self.items:
            claimed |= item.tokens
        return frozenset(claimed)

    @classmethod
    def from_texts(cls, *texts: str) -> SourceCorpus:
        """Index flat text. Every token is shared, so scoping is a no-op.

        Kept for callers that hold no structure — a pasted résumé, a test
        fixture. Prefer `from_resume`, which can tell one employer from
        another.
        """
        tokens: set[str] = set()
        joined: list[str] = []
        for raw in texts:
            if not raw:
                continue
            joined.append(raw.lower())
            tokens |= _index(raw)
        return cls(tokens=tokens, text="\n".join(joined), shared=frozenset(tokens))

    @classmethod
    def from_resume(cls, resume: ParsedResume, *extra_texts: str) -> SourceCorpus:
        """Index a parsed résumé, keeping each job and project separable.

        `extra_texts` is verified outside material — GitHub projects — and is
        indexed as its own item rather than shared, for the same reason a job
        is: a project's stack does not belong on an employer's bullet.
        """
        items: list[CorpusItem] = []
        shared_texts: list[str] = [*resume.preamble]

        contact = resume.contact
        shared_texts += [v for v in (contact.name, contact.email, contact.phone) if v]
        shared_texts += contact.links

        for name, lines in resume.sections.items():
            if name not in _ATTRIBUTED_SECTIONS:
                shared_texts.extend(lines)
                continue
            for position, entry in enumerate(_split_entries(lines)):
                items.append(
                    CorpusItem(
                        ref=f"{name}:{position}",
                        text=entry,
                        tokens=frozenset(_index(entry)),
                    )
                )

        for position, extra in enumerate(t for t in extra_texts if t):
            items.append(
                CorpusItem(ref=f"external:{position}", text=extra, tokens=frozenset(_index(extra)))
            )

        shared = frozenset(_index("\n".join(shared_texts)))
        tokens = set(shared)
        for item in items:
            tokens |= item.tokens

        # Local import: `technologies` reads `bullets.classify`, which imports
        # `parse`, and `parse` is imported here. Deferred the same way the
        # alias table is in `_index` above.
        from packages.tailor.technologies import inventory

        full = "\n".join([*resume.raw_lines, *extra_texts])
        return cls(
            tokens=tokens,
            text=full.lower(),
            items=tuple(items),
            shared=shared,
            technologies=inventory(resume),
        )

    def locate(self, snippet: str) -> CorpusItem | None:
        """The item a piece of source text came from, or None if unknown.

        None is not a failure to report on its own — flat corpora have no
        items — but it does mean the caller gets a document-wide check, and
        `GuardReport.scope_ref` records that so a wide check is never mistaken
        for a narrow one.
        """
        needle = " ".join(snippet.split()).lower()
        if not needle:
            return None
        for item in self.items:
            if needle in " ".join(item.text.split()).lower():
                return item
        return None

    def supports(self, entity: Entity, scope: CorpusItem | None = None) -> bool:
        """Whether `entity` is backed by the source, within `scope` if given.

        ## Shared material does not launder a sibling's claim

        §2.1 permits a rewrite to inject a keyword "already supported by that
        résumé entry **or a shared source section**", and it separately forbids
        borrowing one entry's skill onto another. A Skills list makes those two
        clauses contradict each other, and the second one lost: every
        technology named in Skills became available to every employer, so a
        bullet could say the owner used Kubernetes at a job that never touched
        it — sibling borrowing, with the skills list as the alibi.

        Found by running the guard over a rewritten résumé: scoped to an
        employer that never used it, `Built Python services on Kubernetes`
        passed while the Skills section existed and was correctly refused when
        it was deleted. Same claim, same scope, opposite verdicts, decided by
        an unrelated section of the document.

        So a shared token is withdrawn when some *entry* claims it. The
        distinction is between "the owner knows this" and "the owner did this
        here", which is the distinction the whole scoping mechanism exists for:

        - In this entry — supported, as before.
        - In Skills only, claimed by no entry — still supported anywhere. This
          is §2.1's shared-section allowance and it is untouched; a skill the
          owner lists but never attributed to a job can go anywhere.
        - In Skills *and* in another entry — refused here. The résumé has
          already said where it belongs.

        An unscoped check is unchanged: it asks whether the fact is true of the
        owner at all, and the answer does not depend on attribution.
        """
        available = (
            self.tokens if scope is None else (scope.tokens | (self.shared - self.attributed))
        )

        if entity.normalized in available or singular(entity.normalized) in available:
            return True
        # A hyphenated or slashed compound is supported when each part is.
        parts = [p for p in re.split(r"[-/]", entity.normalized) if p]
        return len(parts) > 1 and all(part in available for part in parts)


def _split_entries(lines: list[str]) -> list[str]:
    """Group a section's lines into entries, one per employer or project."""
    entries: list[list[str]] = []
    for line in lines:
        if _BULLET_RE.match(line) and entries:
            entries[-1].append(line)
        else:
            entries.append([line])
    return ["\n".join(entry) for entry in entries]


def _spelled_out_forms(
    output: str, corpus: SourceCorpus, scope: CorpusItem | None
) -> frozenset[str]:
    """Words the output may use because it wrote out what the source shortened.

    `_index` already handles the indexing direction: a source saying "machine
    learning" makes the token `ml` available, via `aliases.expand_phrases`. The
    mirror was missing. `expand_tokens` deliberately keeps phrases out of a
    token index — "a phrase is not a token and would never match one" — so a
    source saying `ML` never made "machine" or "learning" available, and an
    output spelling the term out failed on its individual words.

    That gap was invisible while the guard matched on capitalization, because
    "machine learning" is lowercase and carried no entity at all. The
    noun-phrase extractor reads every noun, so it surfaced immediately — as a
    fabrication verdict on a rewrite that used the résumé's own vocabulary,
    which is the failure `packages/tailor/aliases.py` exists to prevent.

    Only true equivalences pass, because only the alias table is consulted: the
    output has to contain a phrase whose group the source already asserts in
    some other form. §2.1 is untouched — the claim is identical and only the
    spelling changed, which is the argument the alias table's own docstring
    makes for the other direction.
    """
    from packages.tailor.aliases import phrase_groups

    normalized = " ".join(normalize(m.group(0)) for m in _TOKEN_RE.finditer(output))
    if not normalized:
        return frozenset()

    available = corpus.tokens if scope is None else (scope.tokens | corpus.shared)
    covered: set[str] = set()
    for phrase, forms in phrase_groups():
        if phrase not in normalized:
            continue
        if any(form in available for form in forms if " " not in form):
            covered |= set(phrase.split())
    return frozenset(covered)


def check(output: str, corpus: SourceCorpus, *, scope: CorpusItem | None = None) -> GuardReport:
    """Verify every claim in `output` traces to `corpus`.

    With a `scope`, each claim must trace to that item or to the shared
    material, not merely to somewhere in the document. That is the difference
    between "the owner did this" and "the owner did this *here*", and only the
    second is what a rewritten bullet asserts.
    """
    entities = extract_entities(output)
    reason = (
        "does not appear in the source material"
        if scope is None
        else f"does not appear in {scope.ref} or the shared sections"
    )
    spelled_out = _spelled_out_forms(output, corpus, scope)
    violations = [
        Violation(entity=entity, reason=reason)
        for entity in entities
        if not corpus.supports(entity, scope) and entity.normalized not in spelled_out
    ]
    return GuardReport(
        ok=not violations,
        violations=violations,
        checked=len(entities),
        extractor="noun-phrase" if chunker_available() else "capitalization",
        scope_ref=None if scope is None else scope.ref,
    )


def check_or_raise(
    output: str, corpus: SourceCorpus, *, scope: CorpusItem | None = None
) -> GuardReport:
    """Same, but refuse to return output that fabricates."""
    report = check(output, corpus, scope=scope)
    if not report.ok:
        raise FabricationError(report)
    return report
