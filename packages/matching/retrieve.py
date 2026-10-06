"""Find the postings a chat question is about.

The assistant (CLAUDE.md §14) answers from context it is handed, and until
this module that context was fixed: status counts, profiles, one application,
three replies. A question about the postings themselves — "which open roles
use Kafka?" — had nothing to be answered from, so the honest reply was "I do
not know" and the likely one was invented. This is the retrieval half of
retrieval-augmented generation: the postings a question is about go into the
prompt with a label the model cites.

## Only when the question is about postings

"What needs me?" is about the owner's applications. The first version of this
searched anyway and handed the model five HelloFresh customer-care roles from
Manila, because a nearest-neighbour search always returns its nearest few.
Corpus rarity cannot tell the two kinds of question apart — "me", "waiting"
and "replies" are rare in job postings precisely because postings do not talk
that way — so the gate is the question's own words: it names jobs, roles,
openings or the like, or it names a company in the registry. Otherwise the
context says the postings were not searched, rather than saying nothing, so
the model cannot read the silence as "no matches".

## Keywords decide what is relevant; embeddings decide the order

Measured on the owner's database on 2026-09-30, a pure vector search for
"Which open roles work with Kafka?" returned one posting that mentions Kafka
and four that share only "open" and "roles" with the question. So relevance is
decided by the words: a posting must contain one of the question's
distinguishing terms, weighted by how rare the term is across the corpus
(`idf.DocumentFrequencies`), matched on word boundaries so "rust" does not
match "trust". This scan reads every open posting, including the third of the
corpus with no usable vector — none yet, or one with no model stamp — which no
vector search can see.

Embeddings then re-order those matches, fused with the keyword order by
Reciprocal Rank Fusion. They answer alone only when the question has no
distinguishing term to search for ("any data engineering roles?" — both words
are in most postings). Never as a stand-in when the terms matched nothing:
bge-small's similarity is almost never zero, so "roles using Zig?" would get
the five nearest postings whether or not any of them mention Zig.

## One vector search per embedding space

`description_embedding` does not hold one kind of vector. Each row is stamped
with the model that produced it (`embedding_model`) and, for the weighted
lexical embedder, the corpus statistics it was weighted by
(`embedding_revision`). The owner's database held bge-small and
`lexical-idf@2` vectors side by side, because the matching pass re-embeds in
batches, and a cosine across the two does not fail — it returns a plausible
number. So the question is encoded once per space, each space is searched only
against its own rows, a space nothing here can encode into is left out, and
results from different spaces are combined by rank, never by distance.

## What stays on the machine

The question is embedded locally — lexical hashing or bge-small — and nothing
here calls a provider. The postings returned are employers' public text; where
they go next is the chat route's decision, made under the same per-question
provider choice as the rest of the context.

There is no vector index on the column, so every vector search is an exact
scan. If an ivfflat index is added, a filtered search becomes approximate and
can return fewer rows than asked for.
"""

from __future__ import annotations

import asyncio
import re
import threading
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import NamedTuple

import structlog
from sqlalchemy import ColumnElement, Float, and_, case, exists, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.config import get_settings
from packages.core.models import Application, Company, Posting
from packages.core.models_chunks import PostingChunk
from packages.matching import idf
from packages.matching.embed import (
    Embedder,
    LexicalEmbedder,
    SentenceTransformerEmbedder,
    get_embedder,
    tokenize,
)
from packages.matching.locality import Locality, area_exclusion, locality_of
from packages.matching.rerank import Reranker, get_reranker, reorder
from packages.matching.roles import canonical, named_in

log = structlog.get_logger(__name__)

#: How many postings go into the prompt. Each carries an excerpt, and the
#: local model's context window is the binding constraint: five excerpts is
#: roughly 800 tokens, which leaves room for the rest of the context and the
#: answer in Ollama's default window.
DEFAULT_LIMIT = 5

#: Characters of description quoted per posting. Enough for a requirements
#: paragraph; not so much that five of them crowd out the question.
EXCERPT_CHARS = 600

#: Keyword matches handed to the re-ranking. Larger than `DEFAULT_LIMIT` so the
#: embeddings have something to re-order; small enough that the re-ranking
#: query stays a lookup by id.
KEYWORD_POOL = 30

#: How many further matches a reply lists after the ones the model was shown.
#: The owner asked for "the jobs", and five is what fits in a prompt, not what
#: there is; the dashboard shows these ten at a time.
MORE_LIMIT = 100

#: Keyword matches read when the owner's search area applies, before the area
#: cuts them back to `KEYWORD_POOL`. On the owner's database 11 of 25 answers
#: were abroad, so filtering the 30 best would leave the re-ranking about half
#: a pool, and a posting the area keeps could sit just below 30 foreign ones.
_AREA_POOL = 4 * KEYWORD_POOL

#: The same widening for the vector-only path, as a multiple of the answer.
_AREA_WIDEN = 8

#: Terms searched for per question. A question naming more than eight
#: distinguishing things is rare; a pasted paragraph is not, and every term is
#: one more regex over every open posting.
MAX_TERMS = 8

#: The fusion constant from the RRF paper (Cormack et al., 2009), and the value
#: the owner's own Attorney.AI uses. Borrowed, not tuned: there are no labels
#: for chat retrieval to tune it on.
RRF_K = 60

#: A cosine distance of 1 is a similarity of 0: for a lexical vector, not one
#: term in common. That is no match, and a top-k search would hand it to the
#: model anyway. A definition rather than a tuned threshold, so exactly 1. It
#: only ever bites on lexical vectors — bge-small puts nearly everything above
#: zero — which is why the vector-only path is reserved for questions with no
#: term to search for, rather than trusted to filter itself.
_NO_OVERLAP = 1.0

#: Words that say a question is about postings. Plurals included by hand:
#: `tokenize` drops "job", "role" and "position" as stopwords, so the gate reads
#: the raw words instead.
_POSTING_WORDS_TEXT = """
job jobs role roles posting postings position positions opening openings
vacancy vacancies hiring hire hires listing listings employer employers
company companies
"""
_POSTING_WORDS = frozenset(_POSTING_WORDS_TEXT.split())

#: Words that frame a question rather than describe what it is looking for.
#: "Which open roles use Kafka?" is looking for Kafka; searching for "open"
#: as well matches a quarter of the corpus.
#:
#: The second line is the asker and the asking. Corpus rarity cannot remove
#: these: "me" is in 1.9% of postings, so it scores as distinctive, and "Show
#: me forward deployed engineer jobs." came back as five Pleo postings saying
#: "Show me the benefits!".
#:
#: The third and fourth lines are what the question says a posting does with
#: its subject, and the same trap one word along: "mention" is in 1 open
#: posting and "RAG" in 134, so "Which roles mention RAG?" ranked a posting
#: saying "be sure to mention that bonfires are your jam" first. Each of these
#: is rarer in postings than a typical subject. "use", "require" and "include"
#: are not here because they are common enough to cost nothing.
#:
#: The last line is the owner asking about themselves, measured the same way on
#: 2026-10-04: "missing" is in 98 open postings and Kafka in 124, so "What am I
#: missing for Kafka jobs?" ranked "missing" first and found no Kafka job. Left
#: out on purpose: "lag" (Kafka postings say "consumer lag") and "cv" (computer
#: vision); "learn" and "improve" are in most postings and dropped anyway.
#: The question's own grammar went with it: "am" from "What am I…" matched
#: "I am" and AM for account manager, so the auxiliaries are framing too.
#:
#: And "type" and "kind": the owner asked for "jobs with job types AI
#: engineer" on 2026-10-05. "AI" is in 81% of postings and "engineer" in 39%,
#: so "types" was the one word left to search on, and it found a director of
#: programme management.
_FRAMING_WORDS_TEXT = """
which what who where when how any anything there some open available
current currently apply applied application applications
me my mine myself us show find list give tell get see look looking search
want need interested something
mention mentions mentioned mentioning say says said saying talk talks talking
about regarding related relating refer refers referring referencing contain
contains containing involve involves involving ask asks asked asking
missing lacking lack lacks lagging gap gaps weak weakness weaknesses upskill strengths
qualify resume resumes résumé résumés
am was were been do does did done should could would can shall might must had
im ive ve whats whom whose ought
type types kind kinds
"""
_FRAMING_WORDS = _POSTING_WORDS | frozenset(_FRAMING_WORDS_TEXT.split())

#: bge's documented instruction for a short query against longer passages.
#: Used on chunk searches only, where it was measured: with it, "RAG" found 4
#: of the top 10 chunked postings.
_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

#: Chunks read per chunk-level search before keeping the best per posting.
#: A posting has about fourteen, so this leaves room for `limit` distinct ones.
_CHUNK_POOL = 200

#: Words a company name may drop and still name it: "Mistral" for Mistral AI,
#: "1X" for 1X Technologies.
_COMPANY_SUFFIXES_TEXT = "ai labs lab technologies technology inc corp corporation ltd llc"
_COMPANY_SUFFIXES = frozenset(_COMPANY_SUFFIXES_TEXT.split())

#: How rare the rest of the name must be across postings for it to stand
#: alone. "mistral" is in under 1% of postings; "together", the rest of
#: Together AI, is in a large share, and "how do teams work together" must not
#: filter the search to one company.
_SHORT_NAME_MAX_SHARE = 0.02

#: Above `_SHORT_NAME_MAX_SHARE` and up to this share of *other* companies'
#: postings, a one-word name still means its company when that company has
#: postings open: an employer others mention, not an ordinary word. Measured
#: 2026-10-05: OpenAI 6.6%, Anthropic 4.7%, Stripe 4.4%, Ramp 4.1% elsewhere,
#: against `robotics` 7.4% for a company with nothing open, and "power",
#: "change", "matter", "based" at 18% and up.
_MENTIONED_NAME_MAX_SHARE = 0.10

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_HIRING_MANAGER = re.compile(r"\bhiring managers?\b")

_RAW_WORD = re.compile(r"[a-z0-9+#.]+")
_SENTENCE_START = re.compile(r"(?<=[.!?;])\s+")


class _Hit(NamedTuple):
    posting_id: uuid.UUID
    title: str | None
    location: str | None
    url: str
    description: str | None
    company: str | None


_sentence_transformer: SentenceTransformerEmbedder | None = None
_sentence_transformer_failed = False
#: Encoding runs in a worker thread (see `retrieve`), so two questions at once
#: could otherwise both load the model.
_load_lock = threading.Lock()


@dataclass(frozen=True)
class Passage:
    """One retrieved posting, as the model and the dashboard see it."""

    #: What the model cites: "P1", "P2" …
    label: str
    posting_id: uuid.UUID
    title: str
    company: str | None
    location: str | None
    url: str
    excerpt: str
    #: The owner's application for this posting, if there is one.
    application_status: str | None


@dataclass(frozen=True)
class Listed:
    """A match that did not fit in the prompt: enough to list and link, no excerpt."""

    posting_id: uuid.UUID
    title: str
    company: str | None
    location: str | None
    url: str


@dataclass(frozen=True)
class Retrieval:
    #: False when the question was not about postings and nothing was searched.
    attempted: bool
    passages: tuple[Passage, ...] = ()
    #: Open postings the search could reach. The keyword scan reaches all of
    #: them; a vector-only search reaches the ones embedded in a space this
    #: process can encode into.
    searched: int = 0
    #: Open postings it could not: no vector, or a vector nothing here can
    #: encode a question into. "Nothing found" means less when this is large.
    unsearchable: int = 0
    #: Registry companies the question named, so "nothing found" can say
    #: "no open postings at Mistral AI" rather than leave the model to guess.
    companies: tuple[str, ...] = ()
    #: The owner's search area in words, when it was applied.
    area: str | None = None
    #: The area is on, but the question named a place outside it.
    area_waived: bool = False
    #: Postings that matched the question and were left out for being outside
    #: the area. "None at Faculty" and "none at Faculty where you search" are
    #: different answers.
    outside_area: int = 0
    #: The model that re-ordered the top results, or None when none did: no
    #: model is configured, it failed, or there was nothing to re-order.
    reranked_by: str | None = None
    #: The kinds of job the question named, in words ("machine learning
    #: engineer"), from the same table as the feed's role filter.
    roles: tuple[str, ...] = ()
    #: Their table keys, for a link to the feed filtered the same way.
    role_keys: tuple[str, ...] = ()
    #: How many open postings in the search area carry such a title, or None
    #: when no role was named. The passages are the closest few of these.
    role_total: int | None = None
    #: The next matches after `passages`, in the same order, up to `MORE_LIMIT`.
    more: tuple[Listed, ...] = ()


def _sentence_transformer_embedder() -> SentenceTransformerEmbedder | None:
    """bge-small, loaded once, or None if it cannot be.

    The stored vectors decide which encoder is needed, not
    `EMBEDDING_BACKEND`: a database embedded under one setting outlives a
    change to it. The configured embedder is reused when it already is
    bge-small, so the model is not held in memory twice.
    """
    global _sentence_transformer, _sentence_transformer_failed
    with _load_lock:
        if _sentence_transformer is not None or _sentence_transformer_failed:
            return _sentence_transformer
        configured = get_embedder()
        if isinstance(configured, SentenceTransformerEmbedder):
            _sentence_transformer = configured
            return configured
        try:
            _sentence_transformer = SentenceTransformerEmbedder()
        except Exception as exc:  # noqa: BLE001 - missing package or model
            _sentence_transformer_failed = True
            log.warning("retrieval_cannot_load_sentence_transformer", error=type(exc).__name__)
        return _sentence_transformer


def _encoders(
    spaces: list[tuple[str, int | None]],
    frequencies: idf.DocumentFrequencies,
    active_revision: int | None,
) -> dict[tuple[str, int | None], Embedder]:
    """An embedder for each space that can have a question encoded into it."""
    plain = LexicalEmbedder()
    weighted = LexicalEmbedder(frequencies=frequencies) if frequencies.usable else None

    encoders: dict[tuple[str, int | None], Embedder] = {}
    for model, revision in spaces:
        if model == plain.name:
            encoders[(model, revision)] = plain
        elif weighted is not None and model == weighted.name and revision == active_revision:
            # Only the active weights are stored; vectors from an older
            # revision were built under statistics that no longer exist.
            encoders[(model, revision)] = weighted
        elif model == SentenceTransformerEmbedder.name:
            loaded = _sentence_transformer_embedder()
            if loaded is not None:
                encoders[(model, revision)] = loaded
    return encoders


def _encode(
    encoders: dict[tuple[str, int | None], Embedder], subject: str
) -> dict[tuple[str, int | None], list[float]]:
    """The subject's vector in each space, leaving out any that come back zero.

    A zero vector has no direction: pgvector returns NaN for its distance
    rather than "far".
    """
    if not subject:
        return {}
    return {
        space: vector
        for space, encoder in encoders.items()
        if any(vector := encoder.encode([subject])[0])
    }


def _encode_chunk_queries(
    encoders: dict[tuple[str, int | None], Embedder], subject: str
) -> dict[str, list[float]]:
    """The subject's vector per chunk model, with bge's search prefix where it applies."""
    if not subject:
        return {}
    out: dict[str, list[float]] = {}
    for (model, _), encoder in encoders.items():
        text = (
            _QUERY_PREFIX + subject if isinstance(encoder, SentenceTransformerEmbedder) else subject
        )
        vector = encoder.encode([text])[0]
        if any(vector):
            out[model] = vector
    return out


def _term_pattern(term: str) -> str:
    """A Postgres regex matching `term` as a whole word.

    `\\m` and `\\M` are word-start and word-end. They are only added beside a
    word character: "c++" ends in punctuation, and `\\M` after it would demand
    a word character that is never there.
    """
    escaped = re.escape(term)
    start = r"\m" if term[0].isalnum() else ""
    end = r"\M" if term[-1].isalnum() else ""
    return f"{start}{escaped}{end}"


def _search_area(question: str) -> str | None:
    """The owner's search area in words, or None when it does not apply.

    The feed's standing preference (`SEARCH_US_ONLY`), and on by default for
    the same reason. Waived when the question names a place outside it: "jobs
    in London" is a question about London, and answering "none" to it would
    be filtering out the thing asked for.
    """
    settings = get_settings()
    if not settings.search_us_only or locality_of(question) is Locality.ELSEWHERE:
        return None
    if settings.search_remote_outside_california:
        return "the United States, and outside California remote only"
    return "the United States"


def _outside_area(hit: _Hit) -> bool:
    """The one rule (`locality.area_exclusion`), not a copy of it."""
    return (
        area_exclusion(
            hit.location,
            title=hit.title,
            description=hit.description,
            remote_outside_california=get_settings().search_remote_outside_california,
        )
        is not None
    )


def _search_terms(
    question: str,
    frequencies: idf.DocumentFrequencies,
    *,
    exclude: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    """What the question is looking for, minus the words that frame it.

    With corpus statistics, a term in more than 30% of postings is dropped:
    "engineer" or "data" matches too much of the corpus to say which postings
    a question is about.

    At most `MAX_TERMS`, rarest first. Each term is a regex over every open
    posting, and a message may be 4,000 characters long.
    """
    skip = _FRAMING_WORDS | exclude
    terms = list(dict.fromkeys(t for t in tokenize(question) if t not in skip))
    if frequencies.usable:
        terms = [t for t in terms if frequencies.is_distinguishing(t)]
    # Stable, so without statistics (every idf equal) the question's order holds.
    return sorted(terms, key=frequencies.idf, reverse=True)[:MAX_TERMS]


def _company_aliases(name: str, frequencies: idf.DocumentFrequencies) -> list[set[str]]:
    """The word sets that name a company, each enough on its own.

    The full name; a bracketed alias as a name of its own ("Weights & Biases
    (CoreWeave)" is also "CoreWeave", and "Weights & Biases" without it); and
    either of those without a corporate suffix, when what remains is rare in
    postings. Without corpus statistics nothing is shortened.
    """
    parts = [re.sub(r"\(.*?\)", " ", name), *re.findall(r"\((.*?)\)", name)]
    aliases: list[set[str]] = []
    for part in parts:
        terms = set(tokenize(part))
        if not terms:
            continue
        aliases.append(terms)
        core = terms - _COMPANY_SUFFIXES
        if (
            core
            and core != terms
            and frequencies.usable
            and all(frequencies.document_share(t) <= _SHORT_NAME_MAX_SHARE for t in core)
        ):
            aliases.append(core)
    return aliases


async def _named_companies(
    session: AsyncSession,
    words: set[str],
    frequencies: idf.DocumentFrequencies,
    question: str = "",
) -> tuple[list[uuid.UUID], set[str], list[str]]:
    """Registry companies the question names, the words that named them, and their names.

    A company counts when every word of one of its aliases is in the question
    (`_company_aliases`). Measured on the owner's questions: "jobs at Weights &
    Biases" and "anything at Mistral?" found nothing while only the full
    registry name counted. The words come back too, because a company's name
    is in every one of its postings and so cannot pick out the paragraph of
    any one of them.

    A one-word alias has to pass `_one_word_names_it` as well. The registry
    grew from about 180 curated names to 3,933 when the owner's sheet was
    imported, 108 of them one ordinary word ("Do", "Based", "Fast", "Join"),
    and "N Robotics" tokenizes to `robotics`: "robotics jobs" was read as
    "postings at N Robotics" and found nothing over 103 that say robotics.
    """
    rows = (await session.execute(select(Company.id, Company.name))).all()
    matched: list[tuple[uuid.UUID, str, list[set[str]]]] = []
    for company_id, name in rows:
        hits = [a for a in _company_aliases(name or "", frequencies) if a <= words]
        if hits:
            matched.append((company_id, name, hits))

    # Only companies named by nothing longer than one word need the check, and
    # only they need their own postings counted.
    doubtful = [cid for cid, _, hits in matched if all(len(a) == 1 for a in hits)]
    own: dict[uuid.UUID, int] = {}
    if doubtful and frequencies.usable:
        counted = await session.execute(
            select(Posting.company_id, func.count())
            .where(Posting.company_id.in_(doubtful), Posting.closed_at.is_(None))
            .group_by(Posting.company_id)
        )
        own = {company_id: count for company_id, count in counted.all()}

    named: list[uuid.UUID] = []
    naming: set[str] = set()
    names: list[str] = []
    for company_id, name, hits in matched:
        if all(len(a) == 1 for a in hits) and not any(
            _one_word_names_it(next(iter(a)), own.get(company_id, 0), frequencies, question)
            for a in hits
        ):
            continue
        named.append(company_id)
        naming |= set(tokenize(name or ""))
        names.append(name)
    return named, naming, names


def _one_word_names_it(
    word: str, own_postings: int, frequencies: idf.DocumentFrequencies, question: str
) -> bool:
    """Whether a single word in a question means the company of that name.

    Judged by how common the word is in *other* companies' postings, since a
    company's own postings all carry its name:

    - rare elsewhere: it names the company ("Vanta", "Notion");
    - somewhat common: only for a company with open postings. "OpenAI" is in
      7.9% of postings and "Stripe" in 4.4%, mostly other employers naming a
      tool, and both are real employers here; `robotics` is in 7.4% and
      "N Robotics" has none;
    - common: only after "at" ("jobs at Based"), since "based", "fast" and
      "join" are in half the corpus.

    Without corpus statistics there is nothing to judge by, and the word counts
    as it always did.
    """
    if not frequencies.usable:
        return True
    elsewhere = frequencies.document_share(word) - own_postings / frequencies.total
    if elsewhere <= _SHORT_NAME_MAX_SHARE:
        return True
    if elsewhere <= _MENTIONED_NAME_MAX_SHARE and own_postings > 0:
        return True
    return re.search(rf"(?:\bat|@)\s+{re.escape(word)}\b", question.lower()) is not None


async def _names_a_title(session: AsyncSession, subject: list[str]) -> bool:
    """Every word of the question is in the title of some open posting.

    How a bare job title is recognised: "Forward Deployed Engineer" names no
    job word and no company, and was not searched at all. Requiring every word
    in one title keeps the gate shut for "did the hiring manager reply?",
    whose words share no title.
    """
    if not subject:
        return False
    return bool(
        await session.scalar(
            select(
                exists().where(
                    Posting.closed_at.is_(None),
                    *(Posting.title.regexp_match(_term_pattern(t), flags="i") for t in subject),
                )
            )
        )
    )


async def _titles_in_question(
    session: AsyncSession, question: str, companies: list[uuid.UUID]
) -> list[_Hit]:
    """Open postings whose whole title, words only, is in the question.

    Asked for by name, so they enter the pool whatever the keyword scores say.
    "Director, IT Operations" needed this: "it" is a stopword, and dozens of
    Director and Operations titles tied for the 30 keyword places ahead of
    it. Two words at least, or a posting titled "Engineer" would claim every
    question naming an engineer. Longest first, so the most specific wins.
    """
    asked = " " + " ".join(_NON_ALNUM.split(question.lower())).strip() + " "
    title = func.trim(func.regexp_replace(func.lower(Posting.title), "[^a-z0-9]+", " ", "g"))
    query = (
        select(
            Posting.id,
            Posting.title,
            Posting.location,
            Posting.url,
            Posting.description_raw,
            Company.name,
        )
        .outerjoin(Company, Company.id == Posting.company_id)
        .where(
            Posting.closed_at.is_(None),
            func.strpos(title, " ") > 0,
            func.strpos(literal(asked), func.concat(" ", title, " ")) > 0,
        )
        .order_by(func.length(title).desc(), Posting.first_seen_at.desc(), Posting.id)
        .limit(KEYWORD_POOL)
    )
    if companies:
        query = query.where(Posting.company_id.in_(companies))
    return [_Hit(*row) for row in (await session.execute(query)).all()]


@lru_cache(maxsize=50_000)
def _role_of(title: str) -> str | None:
    """`roles.canonical`, remembered: a role question reads every open title."""
    return canonical(title)


async def _role_hits(
    session: AsyncSession, roles: set[str], companies: list[uuid.UUID]
) -> list[_Hit]:
    """Open postings whose title names one of `roles`, newest first.

    By the alias table, so "Staff AI Engineer" and "Machine Learning Engineer"
    are both found for "AI engineer", and a Product Manager posting that only
    mentions one is not. Titles first, since reading 10,589 descriptions to
    keep 61 would be most of the search's time: 0.1 s this way on the owner's
    corpus, and 0.2 s for the 1,027 software-engineer postings.
    """
    query = select(Posting.id, Posting.title).where(Posting.closed_at.is_(None))
    if companies:
        query = query.where(Posting.company_id.in_(companies))
    query = query.order_by(Posting.first_seen_at.desc(), Posting.id)
    ids = [
        posting_id
        for posting_id, title in (await session.execute(query)).all()
        if _role_of(title or "") in roles
    ]
    found = await _hits_for(session, ids)
    return [found[posting_id] for posting_id in ids if posting_id in found]


async def _keyword_hits(
    session: AsyncSession,
    terms: list[str],
    frequencies: idf.DocumentFrequencies,
    companies: list[uuid.UUID],
    *,
    pool: int = KEYWORD_POOL,
    only: list[uuid.UUID] | None = None,
) -> list[_Hit]:
    """Open postings containing a question term, rarest matches first.

    Two passes, because the obvious single query cost 3.1s for eight terms on
    the owner's 19,018 postings: Postgres rebuilt the concatenated text and ran
    one regex per term on every row. Here one combined regex finds the matches,
    the matched text is materialised once, and the per-term scoring runs only
    over those rows — 0.8s for the same eight terms.
    """
    searchable = func.concat_ws(
        " ", Posting.title, Posting.location, Company.name, Posting.description_raw
    )
    anywhere = "(?:" + "|".join(_term_pattern(term) for term in terms) + ")"
    found = (
        select(Posting.id.label("posting_id"), searchable.label("doc"))
        .outerjoin(Company, Company.id == Posting.company_id)
        .where(Posting.closed_at.is_(None), searchable.regexp_match(anywhere, flags="i"))
    )
    if companies:
        found = found.where(Posting.company_id.in_(companies))
    if only is not None:
        # A role was named: its postings are the ones being asked about.
        found = found.where(Posting.id.in_(only))
    # MATERIALIZED, or Postgres may inline the CTE and rebuild `doc` per term.
    matched = found.cte("matched").prefix_with("MATERIALIZED")

    # A term in the title counts twice: "Forward Deployed Engineer - India"
    # lost to a Director role that only mentioned those words, when the
    # question was the title itself.
    score: ColumnElement[float] = literal(0.0, Float)
    for term in terms:
        weight = frequencies.idf(term)
        hit = matched.c.doc.regexp_match(_term_pattern(term), flags="i")
        in_title = Posting.title.regexp_match(_term_pattern(term), flags="i")
        score = score + case((hit, weight), else_=0.0) + case((in_title, weight), else_=0.0)

    query = (
        select(
            Posting.id,
            Posting.title,
            Posting.location,
            Posting.url,
            Posting.description_raw,
            Company.name,
        )
        .join(matched, matched.c.posting_id == Posting.id)
        .outerjoin(Company, Company.id == Posting.company_id)
        .order_by(score.desc(), Posting.first_seen_at.desc(), Posting.id)
        .limit(pool)
    )
    return [_Hit(*row) for row in (await session.execute(query)).all()]


def _space_filter(model: str, revision: int | None) -> list[ColumnElement[bool]]:
    return [
        Posting.closed_at.is_(None),
        Posting.description_embedding.is_not(None),
        Posting.embedding_model == model,
        Posting.embedding_revision.is_not_distinct_from(revision),
    ]


async def _nearest(
    session: AsyncSession,
    space: tuple[str, int | None],
    vector: list[float],
    limit: int,
    companies: list[uuid.UUID],
) -> list[_Hit]:
    """The nearest open postings in one space, sharing at least something."""
    distance = Posting.description_embedding.cosine_distance(vector)
    query = (
        select(
            Posting.id,
            Posting.title,
            Posting.location,
            Posting.url,
            Posting.description_raw,
            Company.name,
        )
        .outerjoin(Company, Company.id == Posting.company_id)
        .where(*_space_filter(*space), distance < _NO_OVERLAP)
        .order_by(distance, Posting.id)
        .limit(limit)
    )
    if companies:
        query = query.where(Posting.company_id.in_(companies))
    return [_Hit(*row) for row in (await session.execute(query)).all()]


async def _order_in_space(
    session: AsyncSession,
    space: tuple[str, int | None],
    vector: list[float],
    among: list[uuid.UUID],
) -> list[uuid.UUID]:
    """`among`, restricted to this space, nearest first."""
    distance = Posting.description_embedding.cosine_distance(vector)
    rows = await session.scalars(
        select(Posting.id)
        .where(Posting.id.in_(among), *_space_filter(*space))
        .order_by(distance, Posting.id)
    )
    return list(rows.all())


def _chunk_filter(model: str) -> list[ColumnElement[bool]]:
    """Chunks of open postings, from one model, cut from the text the posting holds now."""
    return [
        Posting.closed_at.is_(None),
        PostingChunk.embedding_model == model,
        PostingChunk.content_hash.is_not_distinct_from(Posting.content_hash),
    ]


async def _best_chunks(
    session: AsyncSession,
    model: str,
    vector: list[float],
    *,
    among: list[uuid.UUID] | None = None,
    companies: list[uuid.UUID] | None = None,
    limit: int = _CHUNK_POOL,
) -> list[tuple[uuid.UUID, int, int]]:
    """Postings by their best chunk, nearest first: `(posting_id, start, length)`.

    With `among`, only those postings are ranked: the keyword candidates.
    Without it, the nearest chunks of the whole corpus, then the best per
    posting, above a similarity of zero like `_nearest`.
    """
    distance = PostingChunk.embedding.cosine_distance(vector)
    query = (
        select(PostingChunk.posting_id, PostingChunk.start, PostingChunk.length, distance)
        .join(Posting, Posting.id == PostingChunk.posting_id)
        .where(*_chunk_filter(model))
    )
    if among is not None:
        query = query.where(PostingChunk.posting_id.in_(among))
    else:
        query = query.where(distance < _NO_OVERLAP).order_by(distance).limit(limit)
    if companies:
        query = query.where(Posting.company_id.in_(companies))
    best: dict[uuid.UUID, tuple[float, int, int]] = {}
    for posting_id, start, length, dist in (await session.execute(query)).all():
        if posting_id not in best or dist < best[posting_id][0]:
            best[posting_id] = (dist, start, length)
    ranked = sorted(best.items(), key=lambda item: (item[1][0], str(item[0])))
    return [(posting_id, start, length) for posting_id, (_, start, length) in ranked]


async def _hits_for(session: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, _Hit]:
    rows = await session.execute(
        select(
            Posting.id,
            Posting.title,
            Posting.location,
            Posting.url,
            Posting.description_raw,
            Company.name,
        )
        .outerjoin(Company, Company.id == Posting.company_id)
        .where(Posting.id.in_(ids))
    )
    return {row[0]: _Hit(*row) for row in rows.all()}


def _chunk_excerpt(description: str, start: int, length: int) -> str:
    """The chunk that matched, marked where it was cut from a longer text."""
    from packages.matching.chunks import normalize

    body = normalize(description)
    text = body[start : start + length]
    return ("…" if start > 0 else "") + text + ("…" if start + length < len(body) else "")


def _rerank_pool() -> int:
    return max(0, get_settings().chat_rerank_pool)


def _rerank_text(hit: _Hit, span: tuple[int, int] | None) -> str:
    """What the re-ranker reads for a posting: its best chunk, or its opening.

    The chunk the first model ranked the posting by, because that is the
    arrangement that was measured. A posting with no chunks is read from the
    top, one chunk's worth.
    """
    from packages.matching.chunks import CHUNK_SIZE, normalize

    body = normalize(hit.description or "")
    start, length = span if span is not None else (0, CHUNK_SIZE)
    return body[start : start + length] or (hit.title or "")


async def _reranked(
    reranker: Reranker | None,
    question: str,
    hits: list[_Hit],
    spans: dict[uuid.UUID, tuple[int, int]],
) -> tuple[list[_Hit], str | None]:
    """`hits` with its top `_rerank_pool()` re-ordered, and the model that did it.

    Only ever a re-ordering of what it was given (`rerank.py`). Any failure
    leaves the search's own order: the assistant has to answer either way.
    """
    head = hits[: _rerank_pool()]
    if reranker is None or len(head) < 2:
        return hits, None
    passages = [_rerank_text(hit, spans.get(hit.posting_id)) for hit in head]
    try:
        scores = await asyncio.to_thread(reranker.scores, question, passages)
        return reorder(head, scores) + hits[len(head) :], reranker.name
    except Exception as exc:  # noqa: BLE001 - search must not depend on the second model
        log.warning("rerank_failed", model=reranker.name, error=type(exc).__name__)
        return hits, None


def _fuse(keyword: list[uuid.UUID], by_space: list[list[uuid.UUID]]) -> dict[uuid.UUID, float]:
    """Reciprocal Rank Fusion of the keyword order with each posting's vector order.

    Every posting gets exactly two terms: its keyword rank, and its rank among
    the candidates in its own embedding space. A posting with no vector gets
    the middle rank for the second, not nothing. Scoring it on one term while
    the rest get two meant the strongest keyword match could fall out of the
    top five for the sole reason that the matching pass had not embedded it
    yet — a third of the corpus, on the owner's database.
    """
    neutral = (len(keyword) + 1) / 2
    vector_rank = {
        posting_id: rank for ranking in by_space for rank, posting_id in enumerate(ranking, start=1)
    }
    return {
        posting_id: 1.0 / (RRF_K + rank) + 1.0 / (RRF_K + vector_rank.get(posting_id, neutral))
        for rank, posting_id in enumerate(keyword, start=1)
    }


async def retrieve(
    session: AsyncSession, question: str, *, limit: int = DEFAULT_LIMIT
) -> Retrieval:
    """The open postings `question` is about, and how much of the corpus was read."""
    # Trailing dots off, as `tokenize` does: "Show me remote jobs." ends in
    # "jobs.", and "I applied to Stripe." in "stripe.".
    words = {word.rstrip(".") for word in _RAW_WORD.findall(question.lower())}
    # "Hiring manager" is a person who replies, not a posting. Without this,
    # "did the hiring manager reply?" counted as asking about jobs.
    unframed = _HIRING_MANAGER.sub(" ", question.lower())
    asks_about_jobs = bool({w.rstrip(".") for w in _RAW_WORD.findall(unframed)} & _POSTING_WORDS)
    frequencies, active_revision = await idf.load_active(session)
    companies, company_words, company_names = await _named_companies(
        session, words, frequencies, question
    )
    subject_terms = [t for t in tokenize(question) if t not in _FRAMING_WORDS]
    if not (asks_about_jobs or companies or await _names_a_title(session, subject_terms)):
        return Retrieval(attempted=False)

    open_total = (
        await session.scalar(
            select(func.count()).select_from(Posting).where(Posting.closed_at.is_(None))
        )
        or 0
    )
    space_rows = (
        await session.execute(
            select(Posting.embedding_model, Posting.embedding_revision, func.count())
            .where(
                Posting.closed_at.is_(None),
                Posting.description_embedding.is_not(None),
                Posting.embedding_model.is_not(None),
            )
            .group_by(Posting.embedding_model, Posting.embedding_revision)
            # Largest first, so rank ties go to the space holding more of the
            # corpus and the merge order is reproducible.
            .order_by(func.count().desc(), Posting.embedding_model)
        )
    ).all()
    spaces = [(str(model), revision) for model, revision, _ in space_rows]
    # Off the event loop: the first call loads bge-small, which takes seconds,
    # and every encode is CPU work. §10 wants nothing blocking a handler.
    encoders = await asyncio.to_thread(_encoders, spaces, frequencies, active_revision)
    # Encoded without its framing words, for the same reason they are not
    # searched for: a lexical vector of "which open roles" sits nearest to
    # whatever posting says "open roles", whatever it is about.
    subject = " ".join(subject_terms)
    vectors = await asyncio.to_thread(_encode, encoders, subject)

    # Chunk vectors, where the corpus has them (`matching/chunks.py`): one
    # posting is about fourteen 500-character windows, so a skill named in the
    # requirements is visible here and not to the posting's single vector.
    chunk_models = (
        await session.scalars(
            select(PostingChunk.embedding_model)
            .join(Posting, Posting.id == PostingChunk.posting_id)
            .where(Posting.closed_at.is_(None))
            .distinct()
        )
    ).all()
    chunk_encoders = await asyncio.to_thread(
        _encoders, [(str(model), None) for model in chunk_models], frequencies, active_revision
    )
    chunk_vectors = await asyncio.to_thread(_encode_chunk_queries, chunk_encoders, subject)
    chunk_spans: dict[uuid.UUID, tuple[int, int]] = {}

    terms = _search_terms(question, frequencies)
    if companies:
        # The filter already says "Stripe". Searching for it as well matches
        # every one of its postings equally, so the ones that say "remote" get
        # padded out with ones that do not. Kept only when it is all the
        # question asks, so "jobs at Stripe" still reads every Stripe posting.
        beyond_the_name = _search_terms(question, frequencies, exclude=company_words)
        terms = beyond_the_name or terms
    else:
        beyond_the_name = terms

    # A kind of job, named: "AI engineer", "SRE". Its postings are found by
    # title through the role table, so the words that named it are not also
    # searched for. "AI" is in 81% of the owner's postings and "engineer" in
    # 39%; neither can pick a posting out, and what is left of the question
    # ("using Kafka") is what to look for among that role's postings.
    asked_roles = named_in(question)
    if asked_roles:
        role_words = set().union(*asked_roles.values())
        terms = _search_terms(question, frequencies, exclude=company_words | role_words)

    area = _search_area(question)
    outside = 0
    # By id, because one posting can be found two ways (its role and its exact
    # title) and is still one posting the area left out.
    left_out: set[uuid.UUID] = set()
    role_pool: list[_Hit] | None = None
    if asked_roles:
        of_the_role = await _role_hits(session, set(asked_roles), companies)
        role_pool = [hit for hit in of_the_role if not (area and _outside_area(hit))]
        left_out |= {hit.posting_id for hit in of_the_role} - {hit.posting_id for hit in role_pool}
    # "jobs at Astranis" says nothing to order by: the re-ranker would shuffle
    # one company's postings by their likeness to its name, and take 1.5 s.
    only_names_a_company = bool(companies) and not beyond_the_name and not asked_roles
    # Loading the model blocks for seconds the first time, so off the loop.
    reranker = None if only_names_a_company else await asyncio.to_thread(get_reranker)
    reranked_by: str | None = None
    titled = await _titles_in_question(session, question, companies)
    more_hits: list[_Hit] = []
    if terms or titled or role_pool is not None:
        pool = _AREA_POOL if area else KEYWORD_POOL
        if role_pool is None:
            scanned = (
                await _keyword_hits(session, terms, frequencies, companies, pool=pool)
                if terms
                else []
            )
        else:
            # Among the role's postings only. If none of them says what else
            # was asked, the role alone is the better answer than nothing.
            among_role = [hit.posting_id for hit in role_pool]
            scanned = (
                await _keyword_hits(
                    session, terms, frequencies, companies, pool=len(among_role), only=among_role
                )
                if terms and among_role
                else []
            ) or role_pool
        already = {hit.posting_id for hit in titled}
        scanned = [hit for hit in scanned if hit.posting_id not in already]
        if area:
            # Before the re-ranking, so a foreign posting never takes a place
            # in the pool that an in-area one could have had.
            kept_titled = [hit for hit in titled if not _outside_area(hit)]
            kept_scanned = [hit for hit in scanned if not _outside_area(hit)]
            left_out |= {hit.posting_id for hit in titled + scanned} - {
                hit.posting_id for hit in kept_titled + kept_scanned
            }
            # A role's postings are all listed, so they are not cut to a pool.
            titled = kept_titled
            scanned = kept_scanned if role_pool is not None else kept_scanned[:KEYWORD_POOL]
        keyword = titled + scanned
        among = [hit.posting_id for hit in keyword]
        by_space = [
            await _order_in_space(session, space, vector, among)
            for space, vector in vectors.items()
            if among
        ]
        # Last, so that in `_fuse` a posting with chunks is ranked by its best
        # chunk rather than by its one truncated vector.
        among_spans: dict[uuid.UUID, tuple[int, int]] = {}
        for model, vector in chunk_vectors.items():
            if among:
                best = await _best_chunks(session, model, vector, among=among)
                by_space.append([posting_id for posting_id, _, _ in best])
                among_spans.update({pid: (start, length) for pid, start, length in best})
        fused = _fuse(among, by_space)
        # A posting asked for by its title goes first, longest title first.
        # Then the fused order; the sort is stable, so a tie keeps the keyword
        # order.
        by_title = {hit.posting_id: len(hit.title or "") for hit in titled}
        ordered = sorted(
            keyword, key=lambda hit: (-by_title.get(hit.posting_id, 0), -fused[hit.posting_id])
        )
        # The re-ranker orders the rest. A posting named by its title was
        # asked for outright, and no model's opinion of its text outranks that.
        named = [hit for hit in ordered if hit.posting_id in by_title]
        rest, reranked_by = await _reranked(
            reranker,
            question,
            [hit for hit in ordered if hit.posting_id not in by_title],
            among_spans,
        )
        chosen = (named + rest)[:limit]
        more_hits = (named + rest)[limit : limit + MORE_LIMIT]
        outside = len(left_out)
        searched, unsearchable = open_total, 0
    else:
        width = limit * _AREA_WIDEN if area else limit
        if reranker is not None:
            # Enough candidates for the re-ranker to have something to order.
            width = max(width, _rerank_pool())
        per_space: list[list[_Hit]] = []
        for model, vector in chunk_vectors.items():
            best = (await _best_chunks(session, model, vector, companies=companies))[:width]
            chunk_spans.update({posting_id: (start, length) for posting_id, start, length in best})
            found = await _hits_for(session, [posting_id for posting_id, _, _ in best])
            per_space.append(
                [found[posting_id] for posting_id, _, _ in best if posting_id in found]
            )
        per_space += [
            await _nearest(session, space, vector, width, companies)
            for space, vector in vectors.items()
        ]
        # Rank 1 of every space before rank 2 of any: distances from different
        # models share no scale, and ranks are all they have in common. A
        # posting in both a chunk space and a posting space is taken once.
        ranked, taken = [], set()
        for rank in range(width):
            for hits in per_space:
                if rank < len(hits) and hits[rank].posting_id not in taken:
                    taken.add(hits[rank].posting_id)
                    ranked.append(hits[rank])
        if reranker is None:
            chosen = []
            for hit in ranked:
                if len(chosen) == limit:
                    break
                if area and _outside_area(hit):
                    outside += 1
                    continue
                chosen.append(hit)
        else:
            # The area first, so the re-ranker never spends a place on a
            # posting that would be left out anyway.
            eligible = [hit for hit in ranked if not (area and _outside_area(hit))]
            outside += len(ranked) - len(eligible)
            eligible, reranked_by = await _reranked(reranker, question, eligible, chunk_spans)
            chosen = eligible[:limit]
        shown = {hit.posting_id for hit in chosen}
        more_hits = [
            hit
            for hit in ranked
            if hit.posting_id not in shown and not (area and _outside_area(hit))
        ][:MORE_LIMIT]
        searched = await _reachable(session, list(chunk_vectors), list(vectors))
        unsearchable = max(0, open_total - searched)

    statuses = await _application_statuses(session, [hit.posting_id for hit in chosen])
    passages = tuple(
        Passage(
            label=f"P{index}",
            posting_id=hit.posting_id,
            title=hit.title or "(untitled)",
            company=hit.company,
            location=hit.location,
            url=hit.url,
            excerpt=(
                _chunk_excerpt(hit.description or "", *chunk_spans[hit.posting_id])
                if hit.posting_id in chunk_spans
                else excerpt(
                    hit.description or "",
                    question,
                    ignore=company_words,
                    # For a role question the terms are what was asked beyond
                    # the role, even when that is nothing: quoting on "AI"
                    # finds the employer's own pitch, not the requirements.
                    terms=terms if (terms or role_pool is not None) else None,
                )
            ),
            application_status=statuses.get(hit.posting_id),
        )
        for index, hit in enumerate(chosen, start=1)
    )
    return Retrieval(
        attempted=True,
        passages=passages,
        searched=searched,
        unsearchable=unsearchable,
        companies=tuple(sorted(company_names)),
        area=area,
        area_waived=area is None and get_settings().search_us_only,
        outside_area=outside,
        reranked_by=reranked_by,
        roles=tuple(key.replace("_", " ") for key in sorted(asked_roles)),
        role_keys=tuple(sorted(asked_roles)),
        role_total=len(role_pool) if role_pool is not None else None,
        more=tuple(
            Listed(
                posting_id=hit.posting_id,
                title=hit.title or "(untitled)",
                company=hit.company,
                location=hit.location,
                url=hit.url,
            )
            for hit in more_hits
        ),
    )


async def _reachable(
    session: AsyncSession, chunk_models: list[str], spaces: list[tuple[str, int | None]]
) -> int:
    """Open postings a vector search could reach: chunked, or in a usable space."""
    reach: list[ColumnElement[bool]] = []
    if chunk_models:
        reach.append(
            exists().where(
                PostingChunk.posting_id == Posting.id,
                PostingChunk.embedding_model.in_(chunk_models),
                PostingChunk.content_hash.is_not_distinct_from(Posting.content_hash),
            )
        )
    reach += [and_(*_space_filter(model, revision)) for model, revision in spaces]
    if not reach:
        return 0
    return int(
        await session.scalar(
            select(func.count())
            .select_from(Posting)
            .where(Posting.closed_at.is_(None), or_(*reach))
        )
        or 0
    )


async def _application_statuses(
    session: AsyncSession, posting_ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not posting_ids:
        return {}
    rows = await session.execute(
        select(Application.posting_id, Application.status)
        .where(Application.posting_id.in_(posting_ids))
        .order_by(Application.created_at)
    )
    # Later rows overwrite earlier ones, so a re-application reads as the
    # latest status.
    return {posting_id: status for posting_id, status in rows.all() if posting_id}


#: Occurrences of one term that may seed a window. A posting repeating a word
#: forty times needs one window near it, not forty.
_NEAR_PER_TERM = 20


def _starts_near(body: str, terms: set[str], width: int) -> list[int]:
    """Word-aligned window starts a quarter-window before each term occurrence."""
    lowered = body.lower()
    starts: list[int] = []
    for term in terms:
        pattern = rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])"
        for match in list(re.finditer(pattern, lowered))[:_NEAR_PER_TERM]:
            at = max(0, match.start() - width // 4)
            space = body.find(" ", at) if at else -1
            starts.append(space + 1 if 0 <= space < match.start() else at)
    return starts


def _requirements_start(text: str) -> int | None:
    """Where a posting's requirements heading begins, in whitespace-collapsed text.

    `experience.heading_kind` is the reader of headings ("Requirements", "What
    we look for", "Minimum qualifications"); on a sample of 1,500 of the
    owner's postings it found one in 1,057. None when there is none.
    """
    from packages.matching.experience import Demand, heading_kind

    offset = 0
    for line in text.splitlines():
        collapsed = " ".join(line.split())
        if not collapsed:
            continue
        if heading_kind(collapsed) is Demand.MANDATORY:
            return offset
        offset += len(collapsed) + 1
    return None


def excerpt(
    text: str,
    question: str,
    *,
    width: int = EXCERPT_CHARS,
    ignore: set[str] | frozenset[str] = frozenset(),
    terms: Iterable[str] | None = None,
) -> str:
    """The `width`-character window of `text` that shares most with the question.

    Descriptions open with the employer describing itself, so the first
    `width` characters are the same paragraph for every posting from one
    company. Windows start at sentence boundaries and are scored by how many
    *distinct* question terms they contain — distinct, so a word the posting
    repeats forty times does not outvote the one line naming the skill.

    Among windows with the same count, one that *opens* on a matching sentence
    wins. Otherwise the earliest window reaching the count does, which is the
    one that ends on the match with a paragraph of preamble in front of it.

    `ignore` is for words every candidate contains. Asked about "remote roles
    at Stripe", "stripe" opens every Stripe posting's preamble, and scoring on
    it quoted "Who we are… About Stripe" for all five results.

    `terms` is what the search looked for, when it looked for something. The
    question's own words include ones the search drops for being in most
    postings, and scoring on those quoted "easy to use" for "Which open roles
    use Kafka?" — two of five Kafka postings were handed to the model without
    the word, and it said they did not mention it.

    A window may also start a little before each place a term occurs, not only
    at a sentence. A requirements list without full stops gave Starburst's RAG
    line no sentence start within 600 characters, so no window could quote it.
    """
    body = " ".join(text.split())
    if len(body) <= width:
        return body

    terms = (set(terms) if terms is not None else set(tokenize(question))) - _FRAMING_WORDS - ignore
    sentences = [0] + [match.end() for match in _SENTENCE_START.finditer(body)]
    starts = sorted(set(sentences) | set(_starts_near(body, terms, width)))
    best_start, best_key = 0, (0, False)
    if terms:
        for start, following in zip(starts, [*starts[1:], len(body)], strict=True):
            hits = len(terms & set(tokenize(body[start : start + width])))
            opens_on_match = bool(terms & set(tokenize(body[start:following])))
            if (hits, opens_on_match) > best_key:
                best_start, best_key = start, (hits, opens_on_match)

    if best_key == (0, False):
        # Nothing to quote on, so the opening would be chosen, and a posting
        # opens with the employer describing itself. What it asks for is the
        # more useful 600 characters, when it has a heading to find them by.
        best_start = _requirements_start(text) or 0

    window = body[best_start : best_start + width]
    if best_start + len(window) < len(body):
        # End on a word, not inside one.
        window = window.rsplit(" ", 1)[0] + "…"
    return ("…" if best_start > 0 else "") + window


__all__ = [
    "DEFAULT_LIMIT",
    "EXCERPT_CHARS",
    "MORE_LIMIT",
    "Listed",
    "Passage",
    "Retrieval",
    "excerpt",
    "retrieve",
]
