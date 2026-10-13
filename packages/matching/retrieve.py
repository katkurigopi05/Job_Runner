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

`CHAT_SEMANTIC_CANDIDATES` is an opt-in experiment: keyword searches also
admit neural chunk candidates before ranking. It can recover paraphrases
with no keyword overlap, and can also admit unrelated results. Company and
area filters still apply; explicit role/title pools are not widened.

What the keywords found keeps the order it has with the experiment off. A
posting whose title holds two of the question's search terms keeps its place
outright (`_named_by_its_title`), and the semantic candidates are merged by
rank with the rest, below it. The first version merged the two whole lists as
equals, and a question that named a job by its title lost that job from the
five shown. `docs/ML_EVALUATION.md` has both measured.

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
from collections.abc import Callable, Iterable
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
from packages.matching.eligibility import Sponsorship
from packages.matching.embed import (
    Embedder,
    LexicalEmbedder,
    SentenceTransformerEmbedder,
    get_embedder,
    tokenize,
)
from packages.matching.locality import Locality, area_exclusion, locality_of
from packages.matching.rerank import Reranker, fuse, get_reranker, reorder
from packages.matching.roles import (
    alias_label,
    canonical,
    display_name,
    kind_of,
    named_in,
    related_to,
)
from packages.matching.roles import normalize as role_words_of
from packages.matching.sponsored import (
    MENTIONS as SPONSORSHIP_MENTIONS,
)
from packages.matching.sponsored import (
    SponsorshipCount,
    asking_words,
    asks_which_sponsor,
    sentence_that_says_so,
    verdict,
)

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

#: How many of the question's search terms a posting's title must hold for the
#: posting to keep its keyword place when a second list or a second model
#: could move it (`_named_by_its_title`). Chosen from 1, 2 and 3 on the 104
#: benchmark questions and then checked on 80 others (`docs/ML_EVALUATION.md`,
#: 2026-10-10): at 1 a paraphrase holds whatever shares a word with it by
#: chance, and at 3 a short title that the question spells out is not held.
#:
#: Two always, including for a question with one search term. Letting the
#: only term be enough was tried and measured on the three benchmark
#: questions that have one: in each the lone term was incidental ("network"
#: in "Customer Experience Manager roles at a global network security
#: company", whose other words are all too common to search for), and it
#: held thirty Network Engineer titles ahead of the job that was asked for.
ANCHOR_TITLE_TERMS = 2

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
#: is rarer in postings than a typical subject.
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
#:
#: The line after it is what joins a role to its subject: "roles *based on*
#: AI", "*focused on* security", "that *require* Kafka". This comment used to
#: say "use", "require" and "include" were left out as common enough to cost
#: nothing. That was true of "use" (59% of postings) only while descriptions
#: were all that was searched. The owner asked "any jobs role based on ai" on
#: 2026-10-06 and got 1 posting where "any roles with AI" got 222: `tokenize`
#: drops "on" and keeps "based", and the title search (below) wants every word
#: asked for in the title. Each of these is in under 0.2% of titles. And
#: "require" (23%), "focused" (25%), "relevant" (27%) and "specific" (28%) sit
#: under the 30% cut-off, so they were searched for in descriptions: "roles
#: focused on security" lost "security" (36%) and found postings that say
#: "focused". Left out on purpose: "field" (1.2% of titles, Field Engineer),
#: "area" (Area Manager) and "level", which says something about the job.
#:
#: And "target": the owner's own matches, in their words. "Which skills am I
#: missing for my target jobs?" was a search for "target" on 2026-10-10, the
#: one word left, and came back as growth-marketing and sales postings that
#: say "target audience". It is in 18.6% of descriptions and one title of
#: 11,147.
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
based base use uses used focus focused focuses focusing require requires
required requiring include includes around within such relevant specific
oriented centric driven
skill skills skilled needs needed needing them
target targets targeted targeting
"""
_FRAMING_WORDS = _POSTING_WORDS | frozenset(_FRAMING_WORDS_TEXT.split())

#: bge's documented instruction for a short query against longer passages.
#: Used on chunk searches only, where it was measured: with it, "RAG" found 4
#: of the top 10 chunked postings.
_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

#: Chunks read per chunk-level search before keeping the best per posting.
#: A posting has about fourteen, so this leaves room for `limit` distinct ones.
_CHUNK_POOL = 200

#: Chunks read when a model that judges is handed the postings nearest the
#: question (`_nearest_unfound`). Enough for a few hundred distinct postings:
#: the area keeps about half of what is found, and the nearest are often the
#: ones the keywords already have.
_WIDEN_CHUNKS = 1500

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
#: "My job applications" is about applications. "job" there says what kind,
#: and alone it made the question one about postings.
_JOB_APPLICATION = re.compile(r"\bjobs?\s+(applications?)\b")

#: One thing under two spellings, each rewritten to the one titles use.
#:
#: Measured on the owner's 11,147 open postings on 2026-10-10: no title says
#: "artificial intelligence" and 510 say "AI". So "Any jobs based on AI?" was
#: a title search and listed 240, and "Which roles focus on artificial
#: intelligence?" was a keyword search on two words rare enough to look for
#: ("artificial" 8.3% of descriptions, "intelligence" 24.4%) and returned
#: Governance Intelligence, Customer Intelligence and a marketing manager.
#:
#: Only this pair, because only this pair can be rewritten one way round and
#: lose nothing: "AI" is in 81% of descriptions, so it is never searched for
#: there, and no title spells it out. "machine learning" and "ML" are not
#: here. Both are in titles (138 and 64) and "ml" is rare enough to be
#: searched for in descriptions, so rewriting either would stop finding the
#: postings that use the other.
_SPELLED_OUT = ((re.compile(r"\bartificial[\s-]+intelligence\b", re.IGNORECASE), "AI"),)

#: The owner asking about their own applications: the noun and a word for
#: where one stands, said in the first person. "me" counts here, unlike in
#: the chat route's rule for protected answers: "applications waiting on me"
#: is the owner's queue. All three, because two are in a job title:
#: "Applications Engineer I" has the word and the numeral.
#:
#: The noun and not the verb. "Which jobs can I apply to that use a message
#: queue?" is about jobs, and with "apply" counted it was searched without
#: "queue". The cost is "jobs I applied to that are waiting on my review",
#: which is still searched for "waiting" and "review".
_APPLICATION_NOUNS = frozenset({"application", "applications"})
#: Not the "I" of "I/O".
_FIRST_PERSON = re.compile(r"\b(?:i(?!/)|i'm|im|i've|ive|my|me|mine|myself)\b")

#: What such a question says about an application, and not about a posting:
#: where it stands. "How many applications need my review, and which jobs use
#: Kafka?" is looking for Kafka. Left alone in any other question, where
#: "code review" is something a job can involve. "queue" and "wait" are not
#: here at all: a job uses one and involves the other, and neither is the
#: name of a place an application stands (`queued` and `waiting` are).
_APPLICATION_STATE_TEXT = """
review reviews reviewed reviewing waiting pending queued status
statuses approve approved approval parked submit submitted
"""
_APPLICATION_STATE = frozenset(_APPLICATION_STATE_TEXT.split())

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
    #: The kind of role its title names ("AI engineer"), when a role was asked for.
    kind: str | None = None


@dataclass(frozen=True)
class Listed:
    """A match that did not fit in the prompt: enough to list and link, no excerpt."""

    posting_id: uuid.UUID
    title: str
    company: str | None
    location: str | None
    url: str
    kind: str | None = None


@dataclass(frozen=True)
class Retrieval:
    #: False when nothing was searched: the question was not about postings,
    #: or (`unnamed`) it was and named nothing to look for.
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
    #: Registry companies whose one-word name the question says and was not
    #: taken to be about: they have nothing open, and other employers'
    #: postings say the word (`_only_others_say_it`). "any posting on postman"
    #: is a search for the word; the context still says Postman is not hiring.
    not_hiring: tuple[str, ...] = ()
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
    #: How many postings were found and put in order: `passages`, `more`, and
    #: any past `MORE_LIMIT`. Not how many match the question. The keyword
    #: scan keeps a pool and stops, so this is a floor, and the count of a
    #: role's or a title word's postings is `role_total` or `title_total`.
    ranked: int = 0
    #: How many of those the second model re-ordered.
    reranked: int = 0
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
    #: The kinds of title that role is made of, each with how many open
    #: postings in the area carry it: the kind that was typed first, then by
    #: count. One role in the table is several kinds to the owner, and the two
    #: largest kinds of AI / ML engineer ask for different things.
    kinds: tuple[tuple[str, int], ...] = ()
    #: Neighbouring roles' table keys, for links to the feed. Never mixed in.
    related: tuple[str, ...] = ()
    #: The question's words that were looked for in titles, as the owner typed
    #: them, when nothing in it was rare enough to look for in descriptions.
    title_words: tuple[str, ...] = ()
    #: How many open postings in the search area have them in the title, or
    #: None when titles were not searched.
    title_total: int | None = None
    #: The question asks about jobs and names no role, skill, company, place
    #: or title: "any jobs?", "my target jobs". Nothing was searched. It used
    #: to be searched with an empty subject, which no vector can be made
    #: from, and reported as "searched 0 of 11,147, 11,147 not searchable
    #: yet": a corpus that could not be read, where the question was what
    #: had nothing in it.
    unnamed: bool = False
    #: Set when the question asked which postings sponsor: what the postings it
    #: was about state, counted. The passages are then only the ones that state
    #: they do (`_stating_sponsorship`).
    sponsorship: SponsorshipCount | None = None


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


def _located_at(hit: _Hit, terms: list[str]) -> bool:
    """Whether the posting's location field holds one of the question's terms, as a word."""
    location = (hit.location or "").lower()
    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", location) for term in terms
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


def _company_aliases(name: str, frequencies: idf.DocumentFrequencies) -> list[tuple[str, ...]]:
    """The words that name a company, in the order it says them, each enough on its own.

    The full name; a bracketed alias as a name of its own ("Weights & Biases
    (CoreWeave)" is also "CoreWeave", and "Weights & Biases" without it); and
    either of those without a corporate suffix, when what remains is rare in
    postings. Without corpus statistics nothing is shortened.
    """
    parts = [re.sub(r"\(.*?\)", " ", name), *re.findall(r"\((.*?)\)", name)]
    aliases: list[tuple[str, ...]] = []
    for part in parts:
        terms = tuple(dict.fromkeys(tokenize(part)))
        if not terms:
            continue
        aliases.append(terms)
        core = tuple(term for term in terms if term not in _COMPANY_SUFFIXES)
        if (
            core
            and core != terms
            and frequencies.usable
            and all(frequencies.document_share(t) <= _SHORT_NAME_MAX_SHARE for t in core)
        ):
            aliases.append(core)
    return aliases


def _says(alias: tuple[str, ...], words: set[str], said: list[str]) -> bool:
    """Whether a question says a company's name: its words, side by side, in its order.

    Every word being somewhere in the question used to be enough. "Roles at an
    AI-native multimodal lakehouse company" holds both words of Native AI, so
    it was a question about Native AI, which has nothing open, and the answer
    was "no open postings at Native AI" over a posting second in the corpus
    for it. `said` is the question through `tokenize`, as the name is, so
    "Weights & Biases" and "Bank of America" still meet themselves.
    """
    if len(alias) == 1:
        return alias[0] in words
    width = len(alias)
    return any(tuple(said[at : at + width]) == alias for at in range(len(said) - width + 1))


async def _named_companies(
    session: AsyncSession,
    words: set[str],
    frequencies: idf.DocumentFrequencies,
    question: str = "",
) -> tuple[list[uuid.UUID], set[str], list[str], list[str]]:
    """Registry companies the question names, the words that named them, and their names.

    And, last, the names of companies it says without being about them: ones
    with nothing open whose word other employers' postings say
    (`_only_others_say_it`).

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
    said = tokenize(question)
    matched: list[tuple[uuid.UUID, str, list[tuple[str, ...]]]] = []
    for company_id, name in rows:
        hits = [a for a in _company_aliases(name or "", frequencies) if _says(a, words, said)]
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
    not_hiring: list[str] = []
    for company_id, name, hits in matched:
        open_here = own.get(company_id, 0)
        if all(len(a) == 1 for a in hits) and not any(
            _one_word_names_it(a[0], open_here, frequencies, question) for a in hits
        ):
            if any(_only_others_say_it(a[0], open_here, frequencies) for a in hits):
                not_hiring.append(name)
            continue
        named.append(company_id)
        naming |= set(tokenize(name or ""))
        names.append(name)
    return named, naming, names, not_hiring


#: What ends a sentence, for `_nothing_follows`.
_SENTENCE_END = frozenset("?.!,;:")


def _nothing_follows(question: str, index: int) -> bool:
    r"""Whether `index` is the end of what was said, bar punctuation.

    This was `re.match(r"\s*(?:[?.!,;:]|$)", question[index:])`, and CodeQL
    reads that as a polynomial pattern on text it cannot vouch for: the slice
    copies the rest of the question for every "at" in it, and `\s*` is then
    scanned over the copy. Measured on 36,000 characters of "at fast" it ran
    in 2ms, so the quadratic was reachable only in theory — but a character
    walk is linear by construction and needs no argument, which is worth more
    here than the two lines it costs. Same answers: a run of space then
    punctuation, or the end of the string, and nothing else.
    """
    while index < len(question) and question[index].isspace():
        index += 1
    return index == len(question) or question[index] in _SENTENCE_END


def _only_others_say_it(word: str, own_postings: int, frequencies: idf.DocumentFrequencies) -> bool:
    """A rare word that names a company with nothing open, and that others' postings say.

    "postman" on 2026-10-12: a company on the owner's sheet with no board and
    no posting, and a tool 32 open postings at 20 other employers ask for.
    The word is in 0.12% of postings, so it read as the company's name, and
    "any posting mentioned postman" was answered "no open postings at
    Postman". A company with nothing open has nothing to show for its name;
    the postings that say the word are the only thing the question can find.
    """
    if not frequencies.usable or own_postings:
        return False
    return 0 < frequencies.document_share(word) <= _SHORT_NAME_MAX_SHARE


def _one_word_names_it(
    word: str, own_postings: int, frequencies: idf.DocumentFrequencies, question: str
) -> bool:
    """Whether a single word in a question means the company of that name.

    Judged by how common the word is in *other* companies' postings, since a
    company's own postings all carry its name:

    - rare elsewhere: it names the company ("Vanta", "Notion"), unless the
      company has nothing open and those others are all that say the word
      (`_only_others_say_it`). Then it is the company only after "at";
    - somewhat common: only for a company with open postings. "OpenAI" is in
      7.9% of postings and "Stripe" in 4.4%, mostly other employers naming a
      tool, and both are real employers here; `robotics` is in 7.4% and
      "N Robotics" has none;
    - common: only after "at" ("jobs at Based"), since "based", "fast" and
      "join" are in half the corpus. And only where a name would stand: typed
      with a capital, or ending what is said. "Governance teams at
      fast-growing tech firms" was a question about a company called Fast.

    Without corpus statistics there is nothing to judge by, and the word counts
    as it always did.
    """
    if not frequencies.usable:
        return True
    elsewhere = frequencies.document_share(word) - own_postings / frequencies.total
    if elsewhere <= _SHORT_NAME_MAX_SHARE and not _only_others_say_it(
        word, own_postings, frequencies
    ):
        return True
    if elsewhere <= _MENTIONED_NAME_MAX_SHARE and own_postings > 0:
        return True
    # Every place it follows "at", not the first: "at fast growing startups,
    # or at Fast" says the word once as a word and once as the name.
    return any(
        after_at.group(1)[:1].isupper() or _nothing_follows(question, after_at.end())
        for after_at in re.finditer(
            rf"(?:\bat|@)\s+({re.escape(word)})(?![\w\u2010-\u2015-])", question, re.IGNORECASE
        )
    )


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


@lru_cache(maxsize=50_000)
def _kind_of(title: str) -> str | None:
    return kind_of(title)


#: The heading for titles that are one of a kind. A list of 157 postings with
#: 98 distinct titles is not a dictionary if every title is its own entry.
OTHER_TITLES = "other titles"


def _stem(word: str) -> str:
    """A word with its ending off, so a question and a title can agree.

    `tokenize` keeps word forms apart, and "data engineering roles" is asking
    for data engineers. Applied to both sides, so it only has to be
    consistent, not correct: "sales" and "sale" are one word here either way.
    """
    for suffix in ("ing", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


@lru_cache(maxsize=50_000)
def _title_stems(title: str) -> frozenset[str]:
    return frozenset(_stem(word) for word in tokenize(title))


async def _title_word_hits(
    session: AsyncSession, stems: set[str], companies: list[uuid.UUID]
) -> list[_Hit]:
    """Open postings whose title has every one of `stems`, newest first.

    For a question with no word rare enough to search descriptions for. "AI"
    is in 81% of the owner's descriptions and 4.5% of their titles, `data` 76%
    and 3.8%, `security` 36% and 3.0%: common where a posting talks, telling
    where it names itself. Reading 10,589 titles this way takes 0.05 s.
    """
    query = select(Posting.id, Posting.title).where(Posting.closed_at.is_(None))
    if companies:
        query = query.where(Posting.company_id.in_(companies))
    query = query.order_by(Posting.first_seen_at.desc(), Posting.id)
    ids = [
        posting_id
        for posting_id, title in (await session.execute(query)).all()
        if stems <= _title_stems(title or "")
    ]
    found = await _hits_for(session, ids)
    return [found[posting_id] for posting_id in ids if posting_id in found]


def _family(title: str) -> str:
    """A title's own words as a heading: level and qualifiers off, no stray marks.

    `roles.normalize` leaves the full stop of "Sr." behind, which made a kind
    called ". product marketing manager" beside "product marketing manager".
    """
    words = [word.strip(".") for word in role_words_of(title).split()]
    return alias_label(" ".join(word for word in words if any(c.isalnum() for c in word)))


def _kind_labels(pool: list[_Hit]) -> dict[uuid.UUID, str]:
    """Each posting's heading in the dictionary of kinds.

    A title the role table recognises is the kind it names ("AI engineer",
    "software engineer"). One it does not is its own words with the level and
    the qualifiers off ("product manager"), when at least two postings share
    them, and `OTHER_TITLES` when it is the only one.
    """
    known = {hit.posting_id: _kind_of(hit.title or "") for hit in pool}
    family = {hit.posting_id: known[hit.posting_id] or _family(hit.title or "") for hit in pool}
    shared: dict[str, int] = {}
    for label in family.values():
        shared[label] = shared.get(label, 0) + 1
    return {
        posting_id: (label if known[posting_id] or (label and shared[label] > 1) else OTHER_TITLES)
        for posting_id, label in family.items()
    }


def _kinds(
    labels: dict[uuid.UUID, str], typed: Callable[[str], int], known: set[str]
) -> list[tuple[str, int]]:
    """The kinds with their counts, in the order the owner reads them.

    `typed` says how much of what was asked a heading holds, and more comes
    first: for "product manager", "product manager" before "engineering
    manager". Then a kind the role table knows before a family of titles, so
    "AI engineer" is ahead of "applied AI architect". Then by size. One-offs
    last.
    """
    counts: dict[str, int] = {}
    for label in labels.values():
        counts[label] = counts.get(label, 0) + 1
    return sorted(
        counts.items(),
        key=lambda item: (
            item[0] == OTHER_TITLES,
            -typed(item[0]),
            item[0] not in known,
            -item[1],
            item[0],
        ),
    )


def _as_typed(question: str, word: str) -> str:
    """A word in the owner's own capitals: "ai" was typed "AI"."""
    found = re.search(rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])", question, re.I)
    return found.group(0) if found else word


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
        # And twice in the location: "software engineer jobs in London"
        # returned roles in San Jose whose employer lists a London office. A
        # word in that field is the posting saying where it is.
        in_location = Posting.location.regexp_match(_term_pattern(term), flags="i")
        score = (
            score
            + case((hit, weight), else_=0.0)
            + case((in_title, weight), else_=0.0)
            + case((in_location, weight), else_=0.0)
        )

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


def _widen_pool() -> int:
    return max(0, get_settings().chat_rerank_widen)


def _rerank_text(hit: _Hit, span: tuple[int, int] | None, *, named: bool = False) -> str:
    """What the re-ranker reads for a posting: its best chunk, or its opening.

    The chunk the first model ranked the posting by, because that is the
    arrangement that was measured. A posting with no chunks is read from the
    top, one chunk's worth.

    `named` puts the title, company and place on a line above it, for a model
    that judges. A chunk is 500 characters from the middle of a posting and
    almost never says what the job is called or who is hiring; a question
    usually does. Read without that line, a cross-encoder made the measured
    questions worse than no re-ranker (`rerank.py`).
    """
    from packages.matching.chunks import CHUNK_SIZE, normalize

    body = normalize(hit.description or "")
    start, length = span if span is not None else (0, CHUNK_SIZE)
    text = body[start : start + length] or (hit.title or "")
    if not named:
        return text
    heading = " | ".join(part for part in (hit.title, hit.company, hit.location) if part)
    return f"{heading}\n{text}" if heading else text


async def _nearest_unfound(
    session: AsyncSession,
    chunk_vectors: dict[str, list[float]],
    companies: list[uuid.UUID] | None,
    *,
    in_area: bool,
    found: set[uuid.UUID],
) -> tuple[list[_Hit], dict[uuid.UUID, tuple[int, int]]]:
    """The postings nearest the question that the keywords did not find, and
    the chunk each is nearest by. For a model that will judge every one.

    Held to what a keyword match is held to: a named company, and the owner's
    search area. One the area leaves out is not counted as left out. That
    count is of postings that matched what was asked, and these have not been
    judged yet.
    """
    want = _widen_pool()
    if want <= 0:
        return [], {}
    spans: dict[uuid.UUID, tuple[int, int]] = {}
    for model, vector in chunk_vectors.items():
        nearest = await _best_chunks(
            session, model, vector, companies=companies, limit=_WIDEN_CHUNKS
        )
        for posting_id, start, length in nearest:
            if posting_id not in found:
                spans.setdefault(posting_id, (start, length))
    # Read no more descriptions than could be wanted: the area keeps about
    # half of what is found, as `_AREA_POOL` allows for.
    read = list(spans)[: want * (4 if in_area else 1)]
    hits = await _hits_for(session, read)
    kept = [
        hits[posting_id]
        for posting_id in read
        if posting_id in hits and not (in_area and _outside_area(hits[posting_id]))
    ][:want]
    return kept, {hit.posting_id: spans[hit.posting_id] for hit in kept}


async def _reranked(
    reranker: Reranker | None,
    question: str,
    hits: list[_Hit],
    spans: dict[uuid.UUID, tuple[int, int]],
    added: list[_Hit] | None = None,
) -> tuple[list[_Hit], str | None]:
    """`hits` with its top `_rerank_pool()` re-ordered, and the model that did it.

    A model that scores likeness only ever re-orders what it was given
    (`rerank.py`), and reads the chunk alone.

    A model that judges reads each posting under its title, and its order is
    fused with the search's. It may also be handed `added`, postings the
    search did not find. One of those is kept only when the model scores it
    above zero, its own line for "this answers the question". What the search
    found stays whatever its score: keywords decided that (§14).

    Any failure leaves the search's own order with nothing added: the
    assistant has to answer either way, and an unjudged candidate is the
    near-topic posting the keyword rule exists to keep out.
    """
    head = hits[: _rerank_pool()]
    if reranker is None:
        return hits, None
    offered = head + (added or []) if reranker.judges else head
    if len(offered) < 2:
        return hits, None
    passages = [
        _rerank_text(hit, spans.get(hit.posting_id), named=reranker.judges) for hit in offered
    ]
    try:
        scores = await asyncio.to_thread(reranker.scores, question, passages)
        if not reranker.judges:
            return reorder(head, scores) + hits[len(head) :], reranker.name
        # Ranked among everything it judged, and only then are the added
        # postings it did not call an answer left out. Its opinion of a
        # keyword match is where that match stands among all of them.
        left_out = {
            hit.posting_id
            for hit, score in zip(offered[len(head) :], scores[len(head) :], strict=True)
            if score <= 0
        }
        fused = [
            hit for hit in fuse(offered, scores, ranked=len(head)) if hit.posting_id not in left_out
        ]
        return fused + hits[len(head) :], reranker.name
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


async def _semantic_candidates(
    session: AsyncSession,
    chunk_vectors: dict[str, list[float]],
    companies: list[uuid.UUID],
    area: str | None,
) -> tuple[list[_Hit], dict[uuid.UUID, tuple[int, int]], set[uuid.UUID]]:
    """Bounded neural candidates, sharing the search's company/area rules.

    `_best_chunks` also excludes closed postings and stale chunks. Lexical
    hashes cannot recover semantic paraphrases and are deliberately excluded.
    """
    model = SentenceTransformerEmbedder.name
    vector = chunk_vectors.get(model)
    if vector is None:
        return [], {}, set()
    best = await _best_chunks(session, model, vector, companies=companies)
    hits = await _hits_for(session, [pid for pid, _, _ in best])
    kept: list[_Hit] = []
    spans: dict[uuid.UUID, tuple[int, int]] = {}
    outside: set[uuid.UUID] = set()
    for pid, start, length in best:
        hit = hits.get(pid)
        if hit is None:
            continue
        if area and _outside_area(hit):
            outside.add(pid)
            continue
        kept.append(hit)
        spans[pid] = (start, length)
        if len(kept) == KEYWORD_POOL:
            break
    return kept, spans, outside


def _named_by_its_title(hit: _Hit, terms: list[str]) -> bool:
    """Whether the question's own search terms name this posting in its title.

    Two of them, as whole words (`ANCHOR_TITLE_TERMS`). "What Support Engineer
    I weekend shift roles are open?" names "Support Engineer I (FC, Weekend
    Shift)" this way: `support`, `weekend`, `shift`. A posting named so keeps
    the place the keyword scan gave it. Semantic candidates are merged in
    below it, and the second model is not asked about it. A question with one
    search term names nothing this way.

    Not the same as `_titles_in_question`, which wants the whole title in the
    question and puts it first of all. This is the looser thing a person does
    when they describe a job by some of its title.
    """
    if len(terms) < ANCHOR_TITLE_TERMS:
        return False
    title = (hit.title or "").lower()
    held = sum(
        1 for term in terms if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", title)
    )
    return held >= ANCHOR_TITLE_TERMS


def _fuse_candidates(*rankings: list[uuid.UUID]) -> dict[uuid.UUID, float]:
    """Fuse independent candidate lists; a missing hit gets no invented rank."""
    scores: dict[uuid.UUID, float] = {}
    for ranking in rankings:
        for rank, pid in enumerate(dict.fromkeys(ranking), start=1):
            scores[pid] = scores.get(pid, 0.0) + 1.0 / (RRF_K + rank)
    return scores


#: Keyword matches read for their verdict when a question asks which jobs
#: sponsor and also names something to look for. Wider than `KEYWORD_POOL`:
#: an offer is rare (15 of 5,204 in the owner's area), and one cut off by a
#: pool of thirty is one the answer says does not exist.
_SPONSOR_POOL = 500


async def _stating_sponsorship(
    session: AsyncSession,
    question: str,
    *,
    frequencies: idf.DocumentFrequencies,
    companies: list[uuid.UUID],
    company_words: set[str],
    company_names: list[str],
    not_hiring: list[str],
    open_total: int,
    limit: int,
) -> Retrieval:
    """The postings that state they sponsor, for a question that asks which do.

    "jobs that sponsor H-1B" was a search for those words, and on the owner's
    database every one of the five shown stated that it does not. What a
    posting states is read by `eligibility.py`; this asks it.

    The postings the question is about are found by what else it names: a
    role's postings, the ones that say a word it asked for, or, naming
    nothing, every posting that mentions sponsorship at all. Each is read, and
    only an explicit offer is listed. Silence and a statement that cannot be
    resolved are counted and not shown: unknown is never a pass (§16, §18).

    The counts are of that same set, so the answer can say "one of four data
    engineer postings states it sponsors, one states it does not", which is
    the true answer when the list is short.
    """
    area = _search_area(question)
    asked_roles = named_in(question)
    role_words = {
        word for aliases in asked_roles.values() for alias in aliases for word in alias.split()
    }
    terms = _search_terms(
        question, frequencies, exclude=company_words | role_words | asking_words(question)
    )

    mentions_only = False
    if asked_roles:
        pool = await _role_hits(session, set(asked_roles), companies)
        over = " or ".join(display_name(key) for key in sorted(asked_roles)) + " postings"
        if terms and pool:
            pool = await _keyword_hits(
                session,
                terms,
                frequencies,
                companies,
                pool=len(pool),
                only=[hit.posting_id for hit in pool],
            )
            over += " that say " + " or ".join(f'"{term}"' for term in terms)
    elif terms:
        pool = await _keyword_hits(session, terms, frequencies, companies, pool=_SPONSOR_POOL)
        over = "postings that say " + " or ".join(f'"{term}"' for term in terms)
    else:
        mentions_only = True
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
                Posting.description_raw.op("~*")(SPONSORSHIP_MENTIONS),
            )
            .order_by(Posting.first_seen_at.desc(), Posting.id)
        )
        if companies:
            query = query.where(Posting.company_id.in_(companies))
        pool = [_Hit(*row) for row in (await session.execute(query)).all()]
        over = "open postings that mention sponsorship or a visa"

    inside = [hit for hit in pool if not (area and _outside_area(hit))]
    over += " in the search area" if area else ""
    stated = {hit.posting_id: verdict(hit.description) for hit in inside}
    offering = [hit for hit in inside if stated[hit.posting_id] is Sponsorship.AVAILABLE]
    refused = sum(1 for value in stated.values() if value is Sponsorship.UNAVAILABLE)

    chosen = offering[:limit]
    statuses = await _application_statuses(session, [hit.posting_id for hit in chosen])
    return Retrieval(
        attempted=True,
        passages=tuple(
            Passage(
                label=f"P{index}",
                posting_id=hit.posting_id,
                title=hit.title or "(untitled)",
                company=hit.company,
                location=hit.location,
                url=hit.url,
                excerpt=sentence_that_says_so(hit.description),
                application_status=statuses.get(hit.posting_id),
            )
            for index, hit in enumerate(chosen, start=1)
        ),
        searched=open_total,
        companies=tuple(sorted(company_names)),
        not_hiring=tuple(sorted(not_hiring)),
        area=area,
        area_waived=area is None and get_settings().search_us_only,
        outside_area=len(pool) - len(inside),
        ranked=len(offering),
        more=tuple(
            Listed(
                posting_id=hit.posting_id,
                title=hit.title or "(untitled)",
                company=hit.company,
                location=hit.location,
                url=hit.url,
            )
            for hit in offering[limit : limit + MORE_LIMIT]
        ),
        sponsorship=SponsorshipCount(
            offered=len(offering),
            refused=refused,
            unstated=len(inside) - len(offering) - refused,
            over=over,
            mentions_only=mentions_only,
        ),
    )


async def retrieve(
    session: AsyncSession, question: str, *, limit: int = DEFAULT_LIMIT
) -> Retrieval:
    """The open postings `question` is about, and how much of the corpus was read."""
    for spelled_out, as_titles_say_it in _SPELLED_OUT:
        question = spelled_out.sub(as_titles_say_it, question)
    # Trailing dots off, as `tokenize` does: "Show me remote jobs." ends in
    # "jobs.", and "I applied to Stripe." in "stripe.".
    words = {word.rstrip(".") for word in _RAW_WORD.findall(question.lower())}
    # "Hiring manager" is a person who replies, not a posting. Without this,
    # "did the hiring manager reply?" counted as asking about jobs.
    unframed = _JOB_APPLICATION.sub(r" \1 ", _HIRING_MANAGER.sub(" ", question.lower()))
    asks_about_jobs = bool({w.rstrip(".") for w in _RAW_WORD.findall(unframed)} & _POSTING_WORDS)
    # "Do I have any applications that need me to review them?" is about the
    # owner's applications. It named no job and no company, and was searched
    # anyway: "review" is in a title, and a question whose words are all in
    # one title is taken for the title (below). The same question worded
    # "waiting for my review" was not searched, since no title says "waiting".
    about_my_applications = (
        bool(words & _APPLICATION_NOUNS)
        and bool(words & _APPLICATION_STATE)
        and bool(_FIRST_PERSON.search(question.lower()))
    )
    aside = _APPLICATION_STATE if about_my_applications else frozenset()
    frequencies, active_revision = await idf.load_active(session)
    companies, company_words, company_names, not_hiring = await _named_companies(
        session, words, frequencies, question
    )
    subject_terms = [t for t in tokenize(question) if t not in _FRAMING_WORDS and t not in aside]
    if not (
        asks_about_jobs
        or companies
        or (not about_my_applications and await _names_a_title(session, subject_terms))
    ):
        return Retrieval(attempted=False)
    # Asked for by its whole title, whatever its words are: found before the
    # check below, so a posting titled "Open Application" is still a subject.
    titled = await _titles_in_question(session, question, companies)
    if not (subject_terms or companies or titled):
        return Retrieval(attempted=False, unnamed=True)

    open_total = (
        await session.scalar(
            select(func.count()).select_from(Posting).where(Posting.closed_at.is_(None))
        )
        or 0
    )
    if asks_which_sponsor(question):
        # Answered from what postings state, and not from the words that ask.
        return await _stating_sponsorship(
            session,
            question,
            frequencies=frequencies,
            companies=companies,
            company_words=company_words,
            company_names=company_names,
            not_hiring=not_hiring,
            open_total=open_total,
            limit=limit,
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

    terms = _search_terms(question, frequencies, exclude=aside)
    if companies:
        # The filter already says "Stripe". Searching for it as well matches
        # every one of its postings equally, so the ones that say "remote" get
        # padded out with ones that do not. Kept only when it is all the
        # question asks, so "jobs at Stripe" still reads every Stripe posting.
        beyond_the_name = _search_terms(question, frequencies, exclude=company_words | aside)
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
        typed_aliases = set().union(*asked_roles.values())
        role_words = {word for alias in typed_aliases for word in alias.split()}
        terms = _search_terms(question, frequencies, exclude=company_words | role_words | aside)

    area = _search_area(question)
    outside = 0
    # By id, because one posting can be found two ways (its role and its exact
    # title) and is still one posting the area left out.
    left_out: set[uuid.UUID] = set()
    role_pool: list[_Hit] | None = None
    kinds: list[tuple[str, int]] = []
    label_of: dict[uuid.UUID, str] = {}
    title_words: tuple[str, ...] = ()
    if asked_roles:
        of_the_role = await _role_hits(session, set(asked_roles), companies)
        role_pool = [hit for hit in of_the_role if not (area and _outside_area(hit))]
        left_out |= {hit.posting_id for hit in of_the_role} - {hit.posting_id for hit in role_pool}
        label_of = _kind_labels(role_pool)
        typed_kinds = {alias_label(alias) for alias in typed_aliases}
        kinds = _kinds(label_of, lambda label: int(label in typed_kinds), set(label_of.values()))
    elif not beyond_the_name:
        # Nothing in the question is rare enough to look for in descriptions.
        # "any roles with AI" was answered from vectors, which found employers
        # with AI in their name. Its words are looked for in titles instead,
        # and only if no title has them does the old search stand.
        asked_words = [
            word
            for word in dict.fromkeys(tokenize(question))
            if word not in _FRAMING_WORDS and word not in company_words and word not in aside
        ]
        stems = {_stem(word) for word in asked_words}
        of_the_title = await _title_word_hits(session, stems, companies) if stems else []
        if of_the_title:
            role_pool = [hit for hit in of_the_title if not (area and _outside_area(hit))]
            left_out |= {hit.posting_id for hit in of_the_title} - {
                hit.posting_id for hit in role_pool
            }
            title_words = tuple(_as_typed(question, word) for word in asked_words)
            label_of = _kind_labels(role_pool)
            kinds = _kinds(
                label_of,
                lambda label: len(stems & {_stem(word) for word in tokenize(label)}),
                {kind for hit in role_pool if (kind := _kind_of(hit.title or "")) is not None},
            )
            # With a company named, `terms` fell back to its name. The titles
            # are the search now, so that must not become a keyword scan.
            terms = []
    # The kind that was typed goes first, then the others by size: asked for
    # "AI engineer", the eighteen titled that are not behind forty-one titled
    # "machine learning engineer" because those happen to be newer.
    kind_rank = {kind: rank for rank, (kind, _) in enumerate(kinds)}

    def by_kind(hits: list[_Hit]) -> list[_Hit]:
        if not kind_rank:
            return hits
        return sorted(
            hits,
            key=lambda hit: kind_rank.get(label_of.get(hit.posting_id, ""), len(kind_rank)),
        )

    # "jobs at Astranis" says nothing to order by: the re-ranker would shuffle
    # one company's postings by their likeness to its name, and take 1.5 s.
    only_names_a_company = (
        bool(companies) and not beyond_the_name and not asked_roles and not title_words
    )
    # Loading the model blocks for seconds the first time, so off the loop.
    reranker = None if only_names_a_company else await asyncio.to_thread(get_reranker)
    reranked_by: str | None = None
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
            saying_it = (
                await _keyword_hits(
                    session, terms, frequencies, companies, pool=len(among_role), only=among_role
                )
                if terms and among_role
                else []
            )
            scanned = saying_it or role_pool
            if saying_it:
                # A title that is the role's own name is in every question
                # about the role, so it goes first whatever else was asked:
                # "software engineer jobs in London" opened with three
                # postings titled "Software Engineer" in San Jose that say
                # nothing of London. When something more was asked and some
                # of the role's postings say it, a posting goes first for its
                # title only if it is one of them.
                matching = {hit.posting_id for hit in saying_it}
                titled = [hit for hit in titled if hit.posting_id in matching]
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
        keyword_ids = [hit.posting_id for hit in keyword]
        semantic: list[_Hit] = []
        semantic_spans: dict[uuid.UUID, tuple[int, int]] = {}
        if (
            get_settings().chat_semantic_candidates
            and role_pool is None
            and not only_names_a_company
        ):
            # Unlike the lexical subject, sentence meaning includes the
            # relationships between words. Preserve those for discovery.
            semantic_vectors = await asyncio.to_thread(
                _encode_chunk_queries, chunk_encoders, question
            )
            semantic, semantic_spans, excluded = await _semantic_candidates(
                session, semantic_vectors, companies, area
            )
            left_out |= excluded
            known = set(keyword_ids)
            # Preserve the old excerpts for literal matches. A new semantic
            # match needs the chunk that justified it, not a keyword excerpt
            # cut from a posting that never contained that keyword.
            chunk_spans.update(
                {pid: span for pid, span in semantic_spans.items() if pid not in known}
            )
        # What the keywords found, in the order they are given with the
        # experiment off: the keyword rank fused with each posting's vector
        # rank among themselves. Semantic candidates are merged in below, and
        # never into this.
        by_space = [
            await _order_in_space(session, space, vector, keyword_ids)
            for space, vector in vectors.items()
            if keyword_ids
        ]
        # Last, so that in `_fuse` a posting with chunks is ranked by its best
        # chunk rather than by its one truncated vector.
        among_spans: dict[uuid.UUID, tuple[int, int]] = {}
        for model, vector in chunk_vectors.items():
            if keyword_ids:
                best = await _best_chunks(session, model, vector, among=keyword_ids)
                by_space.append([posting_id for posting_id, _, _ in best])
                among_spans.update({pid: (start, length) for pid, start, length in best})
        fused = _fuse(keyword_ids, by_space)
        # A posting asked for by its title goes first, longest title first.
        # Then the fused order; the sort is stable, so a tie keeps the keyword
        # order.
        by_title = {hit.posting_id: len(hit.title or "") for hit in titled}
        # Between postings with the same title, the one located where the
        # question says. They are found by title and never scored by the
        # keyword scan, so "software engineer jobs in London" put three in
        # San Jose first: the title was all that had been compared.
        there = {hit.posting_id for hit in titled if _located_at(hit, terms)}
        ordered = sorted(
            keyword,
            key=lambda hit: (
                -by_title.get(hit.posting_id, 0),
                hit.posting_id not in there,
                -fused[hit.posting_id],
            ),
        )
        # The re-ranker orders the rest. A posting named by its title was
        # asked for outright, and no model's opinion of its text outranks that.
        named = [hit for hit in ordered if hit.posting_id in by_title]
        loose = [hit for hit in ordered if hit.posting_id not in by_title]
        # Nor does it outrank a posting whose title the question describes
        # (`_named_by_its_title`). Held only when something could move it: a
        # second list, or a model that scores likeness. With neither, this is
        # the order above and nothing is taken out of it. A role's postings
        # are sorted by kind and are not held either.
        #
        # Not from a model that judges. Holding was measured with a likeness
        # model, which re-orders outright and reads no title. The judge was
        # measured the other way: shown every keyword match under its title,
        # its order fused with the search's (`rerank.fuse`), which is its own
        # answer to the same damage. When the two met in one merge the title
        # was held from the judge as well, a third arrangement nobody ran.
        judging = reranker is not None and reranker.judges
        held: list[_Hit] = []
        if role_pool is None and (semantic or (reranker is not None and not judging)):
            held = [hit for hit in loose if _named_by_its_title(hit, terms)]
            kept = {hit.posting_id for hit in held}
            loose = [hit for hit in loose if hit.posting_id not in kept]
        if semantic:
            # The two lists know different things and are merged by rank. The
            # first version merged the whole keyword list this way, so a
            # semantic candidate at rank 1 tied with the best keyword match
            # and the fifth keyword match fell to about tenth.
            set_aside = {hit.posting_id for hit in named + held}
            beside = [hit for hit in semantic if hit.posting_id not in set_aside]
            merged = _fuse_candidates(
                [hit.posting_id for hit in loose], [hit.posting_id for hit in beside]
            )
            seen = {hit.posting_id for hit in loose}
            loose = sorted(
                loose + [hit for hit in beside if hit.posting_id not in seen],
                key=lambda hit: -merged[hit.posting_id],
            )
            # The chunk that found a semantic candidate is the one to read.
            among_spans.update(semantic_spans)
        # A model that judges is also handed the postings nearest the
        # question that the keywords did not find: the same work under
        # another name, and the one the owner's machine has measured. Only
        # where the keywords found something. A vector search cannot say
        # "none" (§20), so candidates are added to what was found and never
        # in its place. And never to a role's own list, which is that role's
        # postings, counted and listed by kind.
        added: list[_Hit] = []
        if reranker is not None and reranker.judges and role_pool is None and keyword:
            added, added_spans = await _nearest_unfound(
                session, chunk_vectors, companies, in_area=bool(area), found=set(keyword_ids)
            )
            among_spans.update(added_spans)
            # Quoted where it matched: it has none of the question's keywords,
            # so a keyword excerpt of it would be its opening.
            chunk_spans.update(added_spans)
        # By kind before the re-ranker, so its thirty places go to what was
        # typed, and again after it, so it orders within a kind and not across.
        handed = by_kind(loose)
        rest, reranked_by = await _reranked(reranker, question, handed, among_spans, added)
        rest = held + by_kind(rest)
        chosen = (named + rest)[:limit]
        more_hits = (named + rest)[limit : limit + MORE_LIMIT]
        ranked_total = len(named) + len(rest)
        # What the second model read: the head of what it was handed, which
        # leaves out the postings held ahead of it, and for one that judges,
        # the unfound postings it was handed beside them.
        reranked_total = min(_rerank_pool(), len(handed)) + len(added) if reranked_by else 0
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
        reranked_total = 0
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
            reranked_total = min(_rerank_pool(), len(eligible)) if reranked_by else 0
        shown = {hit.posting_id for hit in chosen}
        in_area_rest = [
            hit
            for hit in ranked
            if hit.posting_id not in shown and not (area and _outside_area(hit))
        ]
        more_hits = in_area_rest[:MORE_LIMIT]
        ranked_total = len(chosen) + len(in_area_rest)
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
            kind=label_of.get(hit.posting_id),
        )
        for index, hit in enumerate(chosen, start=1)
    )
    return Retrieval(
        attempted=True,
        passages=passages,
        searched=searched,
        unsearchable=unsearchable,
        companies=tuple(sorted(company_names)),
        not_hiring=tuple(sorted(not_hiring)),
        area=area,
        area_waived=area is None and get_settings().search_us_only,
        outside_area=outside,
        reranked_by=reranked_by,
        ranked=ranked_total,
        reranked=reranked_total,
        roles=tuple(display_name(key) for key in sorted(asked_roles)),
        role_keys=tuple(sorted(asked_roles)),
        role_total=len(role_pool) if role_pool is not None and asked_roles else None,
        title_words=title_words,
        title_total=len(role_pool) if role_pool is not None and title_words else None,
        more=tuple(
            Listed(
                posting_id=hit.posting_id,
                title=hit.title or "(untitled)",
                company=hit.company,
                location=hit.location,
                url=hit.url,
                kind=label_of.get(hit.posting_id),
            )
            for hit in more_hits
        ),
        kinds=tuple(kinds),
        related=tuple(
            dict.fromkeys(
                neighbour
                for key in sorted(asked_roles)
                for neighbour in related_to(key)
                if neighbour not in asked_roles
            )
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
