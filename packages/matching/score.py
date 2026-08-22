"""Score postings against a profile, and persist the results as Matches.

Score is cosine similarity between the posting text and the profile's own
material (résumé plus projects), *after* the hard filters have removed
anything disqualifying. A filtered-out posting scores 0.0 and records why —
it never gets a middling score that might sneak past a threshold.

`reasons_json` on every Match carries the breakdown, because a match feed you
cannot interrogate is one you end up ignoring.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Match, Posting, Profile, Project, Resume

if TYPE_CHECKING:
    from packages.matching.legitimacy import Assessment

from packages.matching.embed import Embedder, cosine, get_embedder, tokenize
from packages.matching.filters import apply_filters
from packages.matching.idf import DocumentFrequencies
from packages.matching.salary import compare

log = structlog.get_logger(__name__)

#: Weight of title overlap. A title is short but highly indicative — "Staff
#: Backend Engineer" says more per word than a paragraph of boilerplate.
TITLE_WEIGHT = 0.35
BODY_WEIGHT = 0.65


@dataclass
class ScoredPosting:
    posting_id: str
    score: float
    title_similarity: float = 0.0
    body_similarity: float = 0.0
    excluded_by: list[str] = field(default_factory=list)

    matched_terms: list[str] = field(default_factory=list)
    #: What the posting asks for that the profile does not evidence.
    missing_terms: list[str] = field(default_factory=list)
    #: Legitimacy, from packages/matching/legitimacy.py. Reported beside the
    #: score and never folded into it — "does this fit" and "is this real"
    #: are different questions, and a ghost job written well would otherwise
    #: rank highly *because* it is written well.
    legitimacy: dict[str, object] = field(default_factory=dict)
    #: The score broken into dimensions — packages/matching/rubric.py. An
    #: explanation of the ranking, never an input to it.
    rubric: dict[str, object] = field(default_factory=dict)
    #: Advertised pay against the profile's expectation. A report, never an
    #: answer — §2.2 keeps salary_expectation verbatim for forms.
    salary: dict[str, object] = field(default_factory=dict)

    @property
    def excluded(self) -> bool:
        return bool(self.excluded_by)

    def reasons(self) -> dict[str, object]:
        return {
            "title_similarity": round(self.title_similarity, 4),
            "body_similarity": round(self.body_similarity, 4),
            "excluded_by": self.excluded_by,
            "matched_terms": self.matched_terms,
            "missing_terms": self.missing_terms,
            "legitimacy": self.legitimacy,
            "rubric": self.rubric,
            "salary": self.salary,
        }


async def profile_text(session: AsyncSession, profile: Profile) -> str:
    """Everything the owner has that describes them.

    Résumé plus projects — the same corpus the fabrication guard treats as
    source facts, for the same reason: both are verified material rather than
    anything a model produced.
    """
    parts: list[str] = []

    if profile.base_resume_id:
        resume = await session.get(Resume, profile.base_resume_id)
        if resume and resume.parsed_json:
            lines = resume.parsed_json.get("raw_lines") or []
            parts.extend(str(line) for line in lines)

    projects = (
        await session.scalars(select(Project).where(Project.candidate_id == profile.candidate_id))
    ).all()
    for project in projects:
        parts.append(project.name)
        if project.description:
            parts.append(project.description)
        if project.language:
            parts.append(project.language)
        parts.extend(project.topics_json or [])

    for value in (profile.location, profile.work_auth):
        if value:
            parts.append(str(value))

    return "\n".join(parts)


def _rubric(
    posting: Posting,
    profile: Profile,
    scored: ScoredPosting,
    *,
    target_seniority: str | None,
) -> dict[str, object]:
    """Lazy for the same reason `_assess` is: rubric reads this module's
    ScoredPosting, so a top-level import would be a cycle."""
    from packages.matching.rubric import evaluate

    return evaluate(posting, profile, scored, target_seniority=target_seniority).as_dict()


def _assess(
    posting: Posting,
    *,
    siblings: list[Posting] | None,
    frequencies: DocumentFrequencies | None = None,
) -> Assessment:
    """Imported here rather than at module scope.

    `legitimacy` reads `_BOILERPLATE` and `_proper_nouns` from this module, so
    a top-level import would be a cycle. The dependency runs one way — the
    legitimacy tier uses scoring's vocabulary, and scoring never uses the
    tier — and this keeps it that way.
    """
    from packages.matching.legitimacy import assess

    return assess(posting, siblings=siblings, frequencies=frequencies)


def score_posting(
    posting: Posting,
    profile: Profile,
    profile_vector: list[float],
    embedder: Embedder,
    *,
    target_seniority: str | None = None,
    profile_text_value: str = "",
    siblings: list[Posting] | None = None,
    frequencies: DocumentFrequencies | None = None,
) -> ScoredPosting:
    """Score one posting. Filtered-out postings score exactly 0.0.

    `profile_text_value` is what the vector was built from. Passing it lets
    the score carry the human-readable half — which terms matched, and which
    the posting wants that the profile does not show.
    """
    verdict = apply_filters(profile, posting, target_seniority=target_seniority)
    if not verdict.passed:
        # An excluded posting still gets a rubric. This is the case where the
        # owner most wants to know *why* — a bare 0.0 with no reason reads as
        # a bad match rather than a filtered one.
        excluded = ScoredPosting(posting_id=str(posting.id), score=0.0, excluded_by=verdict.reasons)
        excluded.rubric = _rubric(posting, profile, excluded, target_seniority=target_seniority)
        return excluded

    title_vector = embedder.encode([posting.title or ""])[0]
    body_vector = embedder.encode([posting.description_raw or ""])[0]

    title_similarity = cosine(profile_vector, title_vector)
    body_similarity = cosine(profile_vector, body_vector)

    combined = TITLE_WEIGHT * title_similarity + BODY_WEIGHT * body_similarity

    result = ScoredPosting(
        posting_id=str(posting.id),
        score=round(combined, 6),
        title_similarity=title_similarity,
        body_similarity=body_similarity,
        matched_terms=keyword_overlap(profile_text_value, posting) if profile_text_value else [],
        missing_terms=(
            missing_terms(profile_text_value, posting, frequencies=frequencies)
            if profile_text_value
            else []
        ),
        legitimacy=_assess(posting, siblings=siblings, frequencies=frequencies).as_dict(),
    )
    # Built from the result rather than inside it: every dimension reads
    # fields the scoring above has already filled in.
    result.rubric = _rubric(posting, profile, result, target_seniority=target_seniority)
    result.salary = compare(posting.description_raw or "", profile.salary_expectation).as_dict()
    return result


async def score_and_store(
    session: AsyncSession,
    profile: Profile,
    postings: list[Posting],
    *,
    embedder: Embedder | None = None,
    target_seniority: str | None = None,
    store_excluded: bool = False,
) -> list[ScoredPosting]:
    """Score postings for a profile and upsert Match rows. Does not commit.

    Excluded postings are not stored by default — the feed is for things worth
    looking at, and a table of zeros is noise. Pass `store_excluded=True` when
    debugging why something never appeared.
    """
    active = embedder or get_embedder()
    # Kept, not discarded after encoding: the same text is what the matched
    # and missing term lists are computed against, so the vector and the
    # explanation can never describe different inputs.
    text = await profile_text(session, profile)
    profile_vector = active.encode([text])[0]

    # One pass over the corpus, reused for every posting in it. Built here
    # rather than cached anywhere because it is cheap at this size and a
    # stale copy would silently describe a corpus that no longer exists.
    frequencies = DocumentFrequencies.from_texts(
        f"{p.title or ''}\n{p.description_raw or ''}" for p in postings
    )

    existing = {
        str(match.posting_id): match
        for match in (
            await session.scalars(select(Match).where(Match.profile_id == profile.id))
        ).all()
    }

    scored: list[ScoredPosting] = []
    for posting in postings:
        result = score_posting(
            posting,
            profile,
            profile_vector,
            active,
            target_seniority=target_seniority,
            profile_text_value=text,
            siblings=postings,
            frequencies=frequencies,
        )
        scored.append(result)

        if result.excluded and not store_excluded:
            continue

        match = existing.get(result.posting_id)
        if match is None:
            session.add(
                Match(
                    profile_id=profile.id,
                    posting_id=posting.id,
                    score=result.score,
                    reasons_json=result.reasons(),
                )
            )
        else:
            match.score = result.score
            match.reasons_json = result.reasons()

    await session.flush()
    scored.sort(key=lambda s: s.score, reverse=True)
    log.info(
        "scored_postings",
        profile_id=str(profile.id),
        total=len(postings),
        excluded=sum(1 for s in scored if s.excluded),
        embedder=active.name,
    )
    return scored


async def embed_postings(
    session: AsyncSession,
    postings: list[Posting],
    *,
    embedder: Embedder | None = None,
    revision: int | None = None,
) -> int:
    """Embed postings that lack a vector, or whose vector is from elsewhere.

    "Elsewhere" is the important half. A vector's identity is the embedder that
    produced it *and* the corpus statistics it was weighted by, both recorded
    on the row. A posting embedded by a different model, or under different IDF
    weights, is not in the same space — and cosine across spaces does not fail,
    it returns a plausible number that ranks the feed wrongly and reports
    nothing.

    So a stamp mismatch means re-embed, not compare. Switching
    `EMBEDDING_BACKEND`, or rebuilding the corpus statistics, quietly costs one
    pass over the postings rather than silently costing the ranking.
    """
    active = embedder or get_embedder()
    stamp = getattr(active, "name", "unknown")

    pending = [
        p
        for p in postings
        if p.description_raw
        and (
            p.description_embedding is None
            or p.embedding_model != stamp
            or p.embedding_revision != revision
        )
    ]
    if not pending:
        return 0

    restamped = sum(1 for p in pending if p.description_embedding is not None)
    if restamped:
        log.info(
            "re_embedding_stale_vectors",
            count=restamped,
            model=stamp,
            revision=revision,
        )

    vectors = active.encode([f"{p.title or ''}\n{p.description_raw or ''}" for p in pending])
    for posting, vector in zip(pending, vectors, strict=True):
        posting.description_embedding = vector
        posting.embedding_model = stamp
        posting.embedding_revision = revision

    await session.flush()
    return len(pending)


#: Words that appear in nearly every posting and describe no skill. Without
#: this, "missing" fills up with boilerplate the owner cannot act on.
#:
#: The last group is seniority, which `filters.py` already gates on. Reporting
#: it here too would tell the owner through a second channel about something
#: the hard filters have either passed or excluded on already.
_BOILERPLATE_TEXT = """
about across against all also any around back based benefits best better
build building career culture day design develop developing development
different drive each end ensure environment equal every first focus full
future global grow growth help high impact including insurance join
knowledge lead learn learning level life look make many may more most move
must need needs next now offer one only open others out over own people
plan platform product products provide range remote requirements
responsibilities right scale set skills solutions some support sure take
technology their them these they think time together tools two up us use
user users value values want way well what when where which while who why
within without would write familiarity plus bonus nice

paid time off medical dental vision equity salary compensation

senior junior staff principal mid entry intern internship apprentice
apprenticeship trainee
"""

_BOILERPLATE = frozenset(_BOILERPLATE_TEXT.split())

#: A term the posting says once may be an aside. Twice is emphasis. A term in
#: the title counts regardless — a title has no room for asides.
_MIN_MENTIONS = 2


def _proper_nouns(text: str) -> set[str]:
    """Terms the posting always writes capitalized.

    Position is the obvious signal and the wrong one. Splitting into sentences
    to skip the first word breaks on job descriptions, which are wrapped and
    bulleted, so "the first word of a line" is usually mid-sentence — that is
    how "Experience with\n Prometheus monitoring" hid Prometheus.

    Capitalization *ratio* needs no position. A proper noun is capitalized
    every time it appears; an ordinary word in a posting of any length shows
    up lowercase somewhere. Words that appear once at a sentence start are the
    residual false positive, and stopwords plus boilerplate absorb most of
    those.
    """
    seen: dict[str, int] = {}
    capitalized: dict[str, int] = {}

    for raw in re.findall(r"[A-Za-z][A-Za-z0-9+#.-]*", text):
        bare = raw.rstrip(".")
        if len(bare) < 2:
            continue
        key = bare.lower()
        seen[key] = seen.get(key, 0) + 1
        if bare[0].isupper():
            capitalized[key] = capitalized.get(key, 0) + 1

    return {token for token, total in seen.items() if capitalized.get(token, 0) == total}


def missing_terms(
    profile_text_value: str,
    posting: Posting,
    *,
    limit: int = 12,
    frequencies: DocumentFrequencies | None = None,
) -> list[str]:
    """What the posting emphasizes that the profile does not evidence.

    The complement of `keyword_overlap`, and the more actionable half. §2.1
    forbids the tailorer from adding a skill the résumé does not support, so
    the guard's answer to a gap is a refusal. This turns that refusal into
    information: here is what the posting wants and your résumé does not show,
    for *you* to decide whether it is true of you and worth writing in.

    Nothing here goes near the résumé. It is a report, not an edit.

    With `frequencies`, boilerplate is *measured* rather than read off a hand
    list, and terms are ranked by tf-idf — so a term this corpus puts in every
    posting stops being reported as a gap, which a fixed list cannot know.
    Without it, or when the corpus is too small to be a distribution rather
    than a sample, the hand list stands in.
    """
    profile_tokens = set(tokenize(profile_text_value))
    title_tokens = set(tokenize(posting.title or ""))
    body_tokens = tokenize(posting.description_raw or "")

    counts: dict[str, int] = {}
    order: dict[str, int] = {}
    for position, token in enumerate(body_tokens):
        counts[token] = counts.get(token, 0) + 1
        order.setdefault(token, position)
    for token in title_tokens:
        counts.setdefault(token, 0)
        order.setdefault(token, -1)

    # A named tool said once is still a requirement — "experience with
    # Prometheus" appears one time in most postings. Capitalization mid-text
    # is the same proper-noun signal the fabrication guard reads, so a
    # capitalized term counts at a single mention where an ordinary word
    # needs the repetition to prove it was not an aside.
    proper = _proper_nouns(posting.description_raw or "")

    measured = frequencies is not None and frequencies.usable

    def is_furniture(token: str) -> bool:
        if measured:
            assert frequencies is not None
            return frequencies.is_boilerplate(token)
        return token in _BOILERPLATE

    def is_worth_reporting(token: str) -> bool:
        # Repetition and capitalization are proxies for "does this term carry
        # weight". With a corpus that question is answered directly, so a rare
        # term counts at one mention without needing to be shouted or
        # capitalized — which is how a lowercase tool named once used to slip
        # through unreported.
        if measured:
            assert frequencies is not None
            if frequencies.is_distinguishing(token):
                return True
        return counts[token] >= _MIN_MENTIONS or token in title_tokens or token in proper

    candidates = [
        token
        for token in counts
        if token not in profile_tokens and not is_furniture(token) and is_worth_reporting(token)
    ]

    if measured:
        assert frequencies is not None
        # tf-idf: emphasized *here* and rare across the corpus. A term the
        # posting repeats but every other posting also repeats is not a gap.
        candidates.sort(
            key=lambda token: (-frequencies.weigh(token, max(counts[token], 1)), order[token])
        )
    else:
        # Most-emphasized first; ties broken by where the posting first says it.
        candidates.sort(key=lambda token: (-counts[token], order[token]))
    return candidates[:limit]


def keyword_overlap(profile_text_value: str, posting: Posting) -> list[str]:
    """Terms shared by profile and posting — the human-readable 'why'."""
    profile_tokens = set(tokenize(profile_text_value))
    posting_tokens = tokenize(f"{posting.title or ''} {posting.description_raw or ''}")
    seen: list[str] = []
    for token in posting_tokens:
        if token in profile_tokens and token not in seen:
            seen.append(token)
    return seen[:20]
