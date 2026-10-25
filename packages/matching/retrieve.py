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
from dataclasses import dataclass, field
from functools import lru_cache
from typing import NamedTuple

import structlog
from sqlalchemy import ColumnElement, Float, and_, case, exists, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.config import get_settings
from packages.core.models import Application, Company, Posting
from packages.core.models_chunks import PostingChunk
from packages.matching import idf, tiers
from packages.matching.eligibility import Sponsorship
from packages.matching.embed import (
    Embedder,
    LexicalEmbedder,
    SentenceTransformerEmbedder,
    get_embedder,
    tokenize,
)
from packages.matching.level import (
    AskedLevel,
    LevelCount,
    asked_level,
    at_the_level,
    sift,
    standing,
)
from packages.matching.locality import (
    Locality,
    area_exclusion,
    has_unplaced_location,
    locality_of,
    names_bay_area_office,
    reads_as_remote,
)
from packages.matching.rerank import Reranker, fuse, get_reranker, reorder
from packages.matching.retrieval_trace import stage, traced
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

#: Keyword matches read first when the owner's search area applies, before the
#: area cuts them back to `KEYWORD_POOL`. On the owner's database 11 of 25
#: answers were abroad, so filtering the 30 best would leave the re-ranking
#: about half a pool. When this many still keep fewer than thirty, more are
#: read until they do (`_read_until`): 120 foreign postings above the one here
#: are otherwise an answer of "none in the search area".
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

#: Chunks read first per chunk-level search before keeping the best per
#: posting. A posting has about fourteen, so this leaves room for `limit`
#: distinct ones. Where the area or a level decides which count, the window
#: widens until enough do (`_nearest_kept`).
_CHUNK_POOL = 200

#: Chunks read first when a model that judges is handed the postings nearest
#: the question (`_nearest_unfound`). Enough for a few hundred distinct
#: postings: the area keeps about half of what is found, and the nearest are
#: often the ones the keywords already have. Widened as `_CHUNK_POOL` is.
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

#: What else a shortened name may drop, but only where the question asks about
#: the employer (`_framed_alias`): "Snowflake" for Snowflake Computing. Not in
#: `_COMPANY_SUFFIXES`, where "normal" would name Normal Computing in any
#: question about normal working hours.
_FRAMED_SUFFIXES = _COMPANY_SUFFIXES | frozenset({"computing"})

#: How a question asks about an employer rather than a word, `{w}` the word:
#: "jobs at Snowflake", "Snowflake's", "does Snowflake have", "is Snowflake
#: hiring", "Snowflake jobs". "Postings that require Snowflake" is none of them.
_EMPLOYER_FRAMES = (
    r"(?:\bat|@)\s+{w}\b",
    r"\b{w}['’]s\b",
    r"\bdoes\s+{w}\s+(?:have|hire|offer|post|list)\b",
    r"\b{w}\s+(?:is\s+|are\s+)?hiring\b",
    r"\bis\s+{w}\s+hiring\b",
    r"\b{w}\s+(?:jobs?|roles?|openings?|positions?|postings?|careers?|opportunities)\b",
)

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
#: Question framing is stripped as phrases, after company recognition. "right"
#: in "right to work" and "percentage" as a subject remain searchable.
_PERCENTAGE_REQUEST = re.compile(
    r"\bwhat(?:['’]s|s)?\s+(?:is\s+)?(?:the\s+)?percent(?:age)?\b", re.IGNORECASE
)
_FRAMING_PHRASES = re.compile(
    rf"\bright\s+now\b|\bhow\s+many\b|{_PERCENTAGE_REQUEST.pattern}", re.IGNORECASE
)


def asks_about_own_applications(question: str) -> bool:
    """The shared first-person application-status routing rule (§27)."""
    words = {word.rstrip(".") for word in _RAW_WORD.findall(question.lower())}
    return bool(
        words & _APPLICATION_NOUNS
        and words & _APPLICATION_STATE
        and _FIRST_PERSON.search(question.lower())
    )


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
class PostingShare:
    matching: int
    open_total: int
    scope: str

    @property
    def percentage(self) -> float | None:
        return round(100 * self.matching / self.open_total, 2) if self.open_total else None


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
    #: Exact in-area totals only for one distinguishing term or a company
    #: with no additional subject. These count mentions, not requirements,
    #: using the same area and level predicates as the search.
    match_total: int | None = None
    company_total: int | None = None
    matched_term: str | None = None
    #: Supplied only for a requested percentage with a complete matching count.
    share: PostingShare | None = None
    share_requested: bool = False
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
    #: An explicit Bay Area question narrows the standing search area.
    place_total: int | None = None
    place_counts_complete: bool = False
    search_area_total: int | None = None
    remote_or_unplaced_not_listed: int = 0
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
    #: A title search's postings of a role whose name has the word and whose
    #: own titles do not ("Machine Learning Engineer" for "AI"), listed after
    #: the ones that do and not counted in `title_total` (`tiers.py`).
    related_total: int = 0
    #: What a role question asked beyond the role, as typed: "AI" in "software
    #: engineer roles in AI". The role's postings saying it in the title, and
    #: the ones whose description mentions it when it is rare enough to search
    #: descriptions for (None when it is not), are listed first; the rest of
    #: the role after them, never dropped (`tiers.py`).
    beyond_role: tuple[str, ...] = ()
    beyond_in_title: int = 0
    beyond_mentioned: int | None = None
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
    #: Set when the question named a level ("senior", "entry level"): how the
    #: postings found stand against it by their titles (`matching/level.py`).
    level: LevelCount | None = None


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


def _search_area(place: Locality) -> str | None:
    """The owner's search area in words, or None when it does not apply.

    The feed's standing preference (`SEARCH_US_ONLY`), and on by default for
    the same reason. Waived when the question names a place outside it: "jobs
    in London" is a question about London, and answering "none" to it would
    be filtering out the thing asked for.
    """
    settings = get_settings()
    if not settings.search_us_only or place is Locality.ELSEWHERE:
        return None
    if settings.search_remote_outside_california:
        return "the United States, and outside California remote only"
    return "the United States"


@dataclass
class _PlaceFilter:
    """Count matching in-area postings before narrowing them to Bay Area offices.

    IDs deduplicate the title, keyword and related-role paths. A remote job
    with an explicit Bay Area office is listed; other remote jobs are counted
    separately. Descriptions can establish remote work, never a Bay Area office.
    """

    inside: set[uuid.UUID] = field(default_factory=set)
    bay: set[uuid.UUID] = field(default_factory=set)
    omitted: set[uuid.UUID] = field(default_factory=set)

    def keeps(self, hit: _Hit) -> bool:
        return names_bay_area_office(hit.location, title=hit.title)

    def record(
        self, ident: uuid.UUID, title: str | None, location: str | None, description: str | None
    ) -> bool:
        self.inside.add(ident)
        here = names_bay_area_office(location, title=title)
        if here:
            self.bay.add(ident)
        elif has_unplaced_location(location) or reads_as_remote(
            title=title, location=location, description=description
        ):
            self.omitted.add(ident)
        return here

    def narrow(self, hits: list[_Hit]) -> list[_Hit]:
        return [
            hit
            for hit in hits
            if self.record(hit.posting_id, hit.title, hit.location, hit.description)
        ]


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
    aliases: list[tuple[str, ...]] = []
    for part in _name_parts(name):
        if _loses_a_piece(part):
            # Named only as written (`_written_names`).
            continue
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


def _name_parts(name: str) -> list[str]:
    """The name without its brackets, then what is in them: each a name of its own."""
    return [re.sub(r"\(.*?\)", " ", name), *re.findall(r"\((.*?)\)", name)]


def _loses_a_piece(part: str) -> bool:
    """Whether tokenizing cuts a piece off a hyphenated word of the name.

    "At-Bay" is `bay` once "at" is dropped as a stopword, and "T-robotics" is
    `robotics`: a fragment, and an ordinary word, standing for the whole name.
    "Data engineer jobs in the bay area" was read as a question about At-Bay
    and answered 0 (2026-10-21).
    """
    return any(
        len(tokenize(word)) < len([piece for piece in word.split("-") if piece])
        for word in part.split()
        if "-" in word
    )


def _written_names(name: str) -> list[re.Pattern[str]]:
    """A name tokenizing cuts down, as it has to be written.

    With its hyphen in any case ("at-bay"), or without it only as the registry
    capitalizes it ("At Bay"): "keep cloud costs at bay" is not about At-Bay.
    """
    patterns: list[re.Pattern[str]] = []
    for part in _name_parts(name):
        if not part.strip() or not _loses_a_piece(part):
            continue
        pieces = [[re.escape(p) for p in word.split("-") if p] for word in part.split()]
        hyphened = r"\s+".join("-".join(word) for word in pieces)
        spaced = r"\s+".join(r"\s?".join(word) for word in pieces)
        patterns.append(re.compile(rf"(?<![\w-])(?:(?i:{hyphened})|{spaced})(?![\w-])"))
    return patterns


def _framed_alias(name: str, words: set[str], frequencies: idf.DocumentFrequencies) -> str | None:
    """A one-word shortened name the question says, that names its company only as an employer.

    "Snowflake" for Snowflake Computing is in 3.3% of postings, nearly all of
    them about the product, so it is not a name everywhere (`_company_aliases`
    wants under 2%), and "What jobs does Snowflake have open?" listed Fivetran's
    and GitLab's (2026-10-21). Asked about the employer (`_EMPLOYER_FRAMES`) it
    names the company; "postings that require Snowflake" still means the product,
    which is on the owner's résumé. Up to `_MENTIONED_NAME_MAX_SHARE`, as for a
    one-word name, and the caller wants the company to have postings open.
    """
    if not frequencies.usable:
        return None
    main = _name_parts(name)[0]
    if _loses_a_piece(main):
        return None
    terms = tuple(dict.fromkeys(tokenize(main)))
    core = tuple(term for term in terms if term not in _FRAMED_SUFFIXES)
    if len(core) != 1 or core == terms or core[0] not in words:
        return None
    if frequencies.document_share(core[0]) > _MENTIONED_NAME_MAX_SHARE:
        return None
    return core[0]


def _asks_about_employer(word: str, question: str) -> bool:
    """Whether the question uses `word` as an employer's name (`_EMPLOYER_FRAMES`)."""
    escaped = re.escape(word)
    return any(
        re.search(frame.format(w=escaped), question, re.IGNORECASE) for frame in _EMPLOYER_FRAMES
    )


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
    # Named exactly as written (`_written_names`), and by a shortened name used
    # as an employer's (`_framed_alias`), which also needs postings open.
    written: list[tuple[uuid.UUID, str]] = []
    framed: list[tuple[uuid.UUID, str]] = []
    for company_id, raw_name in rows:
        name = raw_name or ""
        hits = [a for a in _company_aliases(name, frequencies) if _says(a, words, said)]
        if hits:
            matched.append((company_id, name, hits))
        elif any(pattern.search(question) for pattern in _written_names(name)):
            written.append((company_id, name))
        elif (word := _framed_alias(name, words, frequencies)) and _asks_about_employer(
            word, question
        ):
            framed.append((company_id, name))

    # Only companies named by nothing longer than one word need the check, and
    # only they need their own postings counted.
    doubtful = [cid for cid, _, hits in matched if all(len(a) == 1 for a in hits)]
    doubtful += [cid for cid, _ in framed]
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
    for company_id, name in [*written, *((cid, n) for cid, n in framed if own.get(cid, 0))]:
        named.append(company_id)
        naming |= set(tokenize(name))
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
    session: AsyncSession,
    question: str,
    companies: list[uuid.UUID],
    *,
    counts: Callable[[_Hit], bool] | None = None,
) -> list[_Hit]:
    """Open postings whose whole title, words only, is in the question.

    Asked for by name, so they enter the pool whatever the keyword scores say.
    "Director, IT Operations" needed this: "it" is a stopword, and dozens of
    Director and Operations titles tied for the 30 keyword places ahead of
    it. Two words at least, or a posting titled "Engineer" would claim every
    question naming an engineer. Longest first, so the most specific wins.

    `KEYWORD_POOL` of them, or with `counts`, read on until that many count:
    the newest thirty with a title may all be abroad (`_read_until`).
    """
    asked = " " + " ".join(_NON_ALNUM.split(question.lower())).strip() + " "
    title = func.trim(func.regexp_replace(func.lower(Posting.title), "[^a-z0-9]+", " ", "g"))
    query = (
        select(Posting.id)
        .where(
            Posting.closed_at.is_(None),
            func.strpos(title, " ") > 0,
            func.strpos(literal(asked), func.concat(" ", title, " ")) > 0,
        )
        .order_by(func.length(title).desc(), Posting.first_seen_at.desc(), Posting.id)
    )
    if companies:
        query = query.where(Posting.company_id.in_(companies))
    if counts is None:
        query = query.limit(KEYWORD_POOL)
    ranking = list((await session.scalars(query)).all())
    return await _read_until(session, ranking, first=KEYWORD_POOL, counts=counts, want=KEYWORD_POOL)


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
    session: AsyncSession,
    stems: set[str],
    companies: list[uuid.UUID],
    *,
    keep: Callable[[str], bool] | None = None,
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
        if stems <= _title_stems(title or "") and (keep is None or keep(title or ""))
    ]
    found = await _hits_for(session, ids)
    return [found[posting_id] for posting_id in ids if posting_id in found]


async def _related_titles(
    session: AsyncSession,
    stems: frozenset[str],
    companies: list[uuid.UUID],
    *,
    found: list[_Hit],
    area: str | None,
    level: AskedLevel | None,
) -> list[_Hit]:
    """The titles of a role whose name has the words asked and that do not:
    "Machine Learning Engineer" for "any roles for AI", since "AI engineer"
    names that role. In the area and at the level asked, like the rest."""
    roles = tiers.roles_holding(stems, _stem)
    if not roles:
        return []
    have = {hit.posting_id for hit in found}
    related = [
        hit
        for hit in await _role_hits(session, roles, companies)
        if hit.posting_id not in have and not (area and _outside_area(hit))
    ]
    kept, _ = _at_level(related, level)
    return kept


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
    pool: int | None = KEYWORD_POOL,
    only: list[uuid.UUID] | None = None,
) -> list[_Hit]:
    """Open postings containing a question term, rarest matches first.

    The best `pool` of them, or every one with `pool=None`.
    """
    ranking = await _keyword_ranking(session, terms, frequencies, companies, limit=pool, only=only)
    return await _read_until(session, ranking, first=len(ranking))


async def _read_until(
    session: AsyncSession,
    ranking: list[uuid.UUID],
    *,
    first: int,
    counts: Callable[[_Hit], bool] | None = None,
    want: int = 0,
) -> list[_Hit]:
    """`ranking`'s postings in its order, read until `want` of them `counts`.

    The first `first` are read whatever they are, and then twice as many
    again each time, until enough count or the ranking runs out. Every
    posting read is returned, counted or not, so the caller's filters and
    their counts of what was left out work on it as on any pool.

    This is how a filter runs before a cut. A pool cut first and filtered
    after is a posting the filter would keep and the answer says does not
    exist. Reading `first` before anything else keeps a search that was not
    short exactly as it was.
    """
    read: list[_Hit] = []
    kept = 0
    start, size = 0, max(first, 1)
    while start < len(ranking):
        batch = ranking[start : start + size]
        found = await _hits_for(session, batch)
        hits = [found[posting_id] for posting_id in batch if posting_id in found]
        read.extend(hits)
        start += len(batch)
        if counts is None:
            continue
        kept += sum(1 for hit in hits if counts(hit))
        if kept >= want:
            break
        size *= 2
    return read


# A complete denominator revisits the whole corpus. A 4,096-entry cache
# evicted its own metadata before the next request and repeated every place
# parse. Keep it bounded, but large enough for an ordinary full-corpus scan.
@lru_cache(maxsize=32768)
def _metadata_area_exclusion(
    title: str | None, location: str | None, remote_outside: bool
) -> str | None:
    return area_exclusion(
        location,
        title=title,
        description=None,
        remote_outside_california=remote_outside,
    )


@stage("metadata_totals")
async def _matching_totals(
    session: AsyncSession,
    posting_ids: list[uuid.UUID] | None,
    *,
    area: bool,
    level: AskedLevel | None,
    allow_unknown: bool,
    place_filter: _PlaceFilter | None = None,
) -> tuple[int, int, int]:
    """Count the keyword scan's complete match IDs, beyond its ranked pool.

    No second regex scan: word boundaries were already applied. Classify
    metadata first, then fetch descriptions only for on-site
    exclusions outside California, the only verdict prose can change.
    Counts use the search's own area and level rules, without reordering it.
    None selects every open posting for a requested percentage's denominator.
    """
    if posting_ids is None and place_filter is None and level is None:
        return await _open_area_total(session, area=area), 0, 0
    if posting_ids == []:
        return 0, 0, 0
    query = select(
        Posting.id,
        Posting.title,
        Posting.location,
        Posting.company_id,
    ).where(Posting.closed_at.is_(None))
    if posting_ids is not None:
        query = query.where(Posting.id.in_(posting_ids))
    total, outside = 0, 0
    employers: set[uuid.UUID] = set()
    remote_outside = get_settings().search_remote_outside_california
    eligibility: dict[tuple[str | None, str | None], tuple[str | None, bool]] = {}
    recheck: dict[uuid.UUID, tuple[str | None, str | None, uuid.UUID | None, bool]] = {}
    remote_recheck: dict[uuid.UUID, tuple[str | None, str | None]] = {}

    def count(
        ident: uuid.UUID,
        title: str | None,
        location: str | None,
        company_id: uuid.UUID | None,
        description: str | None = None,
    ) -> None:
        nonlocal total
        if place_filter is not None and not place_filter.record(
            ident, title, location, description
        ):
            # Prose can establish that a known non-Bay Californian job is remote.
            if description is None and ident not in place_filter.omitted:
                remote_recheck[ident] = (title, location)
            return
        total += 1
        if company_id is not None:
            employers.add(company_id)

    for ident, title, location, company_id in (await session.execute(query)).all():
        key = (title, location)
        if key not in eligibility:
            reason = _metadata_area_exclusion(title, location, remote_outside) if area else None
            place = standing(title, level) if level is not None else 0
            eligibility[key] = (reason, place < 2 or (allow_unknown and place == 2))
        reason, at_level = eligibility[key]
        if reason is not None and reason.endswith(" is on-site outside California"):
            recheck[ident] = (title, location, company_id, at_level)
            continue
        if reason is not None:
            outside += 1
        elif at_level:
            count(ident, title, location, company_id)
    if recheck:
        descriptions = await session.execute(
            select(Posting.id, Posting.description_raw).where(Posting.id.in_(list(recheck)))
        )
        for ident, description in descriptions.all():
            title, location, company_id, at_level = recheck[ident]
            # Without remote-work prose, this metadata exclusion cannot
            # change. Avoid re-parsing its geography in that common case.
            if not reads_as_remote(title=None, location=None, description=description):
                outside += 1
                continue
            reason = area_exclusion(
                location,
                title=title,
                description=description,
                remote_outside_california=remote_outside,
            )
            if reason is not None:
                outside += 1
            elif at_level:
                count(ident, title, location, company_id, description)
    if remote_recheck and place_filter is not None:
        descriptions = await session.execute(
            select(Posting.id, Posting.description_raw).where(Posting.id.in_(list(remote_recheck)))
        )
        for ident, description in descriptions.all():
            title, location = remote_recheck[ident]
            place_filter.record(ident, title, location, description)
    return total, len(employers), outside


async def _open_area_total(session: AsyncSession, *, area: bool) -> int:
    """The denominator needs a count, without IDs or employers for every row.

    Group identical title/location pairs before reading metadata. A group whose
    on-site verdict prose can flip is still checked one description at a time:
    identical metadata does not mean identical remote-work evidence.
    """
    if not area:
        return int(
            await session.scalar(select(func.count()).where(Posting.closed_at.is_(None))) or 0
        )
    rows = await session.execute(
        select(Posting.title, Posting.location, func.count())
        .where(Posting.closed_at.is_(None))
        .group_by(Posting.title, Posting.location)
    )
    remote_outside = get_settings().search_remote_outside_california
    total = 0
    pending: set[tuple[str | None, str | None]] = set()
    for title, location, count in rows.all():
        reason = _metadata_area_exclusion(title, location, remote_outside)
        if reason is None:
            total += count
        elif reason.endswith(" is on-site outside California"):
            pending.add((title, location))
    if not pending:
        return total
    # A short location prefilter avoids a large tuple-IN disjunction. Check
    # the exact title/location key in Python before considering its prose.
    locations = {location for _, location in pending if location is not None}
    clauses: list[ColumnElement[bool]] = [Posting.location.in_(locations)]
    if any(location is None for _, location in pending):
        clauses.append(Posting.location.is_(None))
    descriptions = await session.execute(
        select(Posting.title, Posting.location, Posting.description_raw).where(
            Posting.closed_at.is_(None), or_(*clauses)
        )
    )
    for title, location, description in descriptions.all():
        if (title, location) not in pending:
            continue
        if not reads_as_remote(title=None, location=None, description=description):
            continue
        if (
            area_exclusion(
                location,
                title=title,
                description=description,
                remote_outside_california=remote_outside,
            )
            is None
        ):
            total += 1
    return total


@stage("keyword")
async def _keyword_ranking(
    session: AsyncSession,
    terms: list[str],
    frequencies: idf.DocumentFrequencies,
    companies: list[uuid.UUID],
    *,
    limit: int | None = None,
    only: list[uuid.UUID] | None = None,
) -> list[uuid.UUID]:
    """Every open posting containing a question term, rarest matches first: ids.

    Ids, so a caller that filters can read as far down as its filter needs
    (`_read_until`). Scoring has to reach every match to order them, so the
    whole order costs what its head did.

    Two passes, because the obvious single query cost 3.1s for eight terms on
    the owner's 19,018 postings: Postgres rebuilt the concatenated text and ran
    one regex per term on every row. Here one combined regex finds the matches,
    the matched text is materialised once, and the per-term scoring runs only
    over those rows — 0.8s for the same eight terms.
    """
    if not terms:
        return []
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
        select(Posting.id)
        .join(matched, matched.c.posting_id == Posting.id)
        .order_by(score.desc(), Posting.first_seen_at.desc(), Posting.id)
        .limit(limit)
    )
    return list((await session.scalars(query)).all())


def _space_filter(model: str, revision: int | None) -> list[ColumnElement[bool]]:
    return [
        Posting.closed_at.is_(None),
        Posting.description_embedding.is_not(None),
        Posting.embedding_model == model,
        Posting.embedding_revision.is_not_distinct_from(revision),
    ]


@stage("dense")
async def _nearest(
    session: AsyncSession,
    space: tuple[str, int | None],
    vector: list[float],
    limit: int,
    companies: list[uuid.UUID],
    *,
    counts: Callable[[_Hit], bool] | None = None,
    want: int = 0,
) -> list[_Hit]:
    """The nearest open postings in one space, sharing at least something.

    `limit` of them. With `counts`, read on past `limit` until `want` of them
    count, for the reason `_nearest_kept` does, and every posting read is
    returned.
    """
    distance = Posting.description_embedding.cosine_distance(vector)
    window = limit
    while True:
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
            .limit(window)
        )
        if companies:
            query = query.where(Posting.company_id.in_(companies))
        hits = [_Hit(*row) for row in (await session.execute(query)).all()]
        if counts is None or len(hits) < window:
            return hits
        kept = 0
        for index, hit in enumerate(hits):
            kept += int(counts(hit))
            if kept >= want and index + 1 >= limit:
                return hits[: index + 1]
        window *= _WIDEN_BY


@stage("dense")
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


@stage("chunks")
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
    best, _ = await _chunk_ranking(
        session, model, vector, among=among, companies=companies, limit=limit
    )
    return best


@stage("chunks")
async def _chunk_ranking(
    session: AsyncSession,
    model: str,
    vector: list[float],
    *,
    among: list[uuid.UUID] | None,
    companies: list[uuid.UUID] | None,
    limit: int,
) -> tuple[list[tuple[uuid.UUID, int, int]], bool]:
    """`_best_chunks`, and whether every chunk it could have read was read."""
    distance = PostingChunk.embedding.cosine_distance(vector)
    query = (
        select(PostingChunk.posting_id, PostingChunk.start, PostingChunk.length, distance)
        .join(Posting, Posting.id == PostingChunk.posting_id)
        .where(*_chunk_filter(model))
    )
    if among is not None:
        query = query.where(PostingChunk.posting_id.in_(among))
    else:
        # A total order, so a wider window starts with the narrower one.
        query = (
            query.where(distance < _NO_OVERLAP)
            .order_by(distance, PostingChunk.posting_id, PostingChunk.ordinal)
            .limit(limit)
        )
    if companies:
        query = query.where(Posting.company_id.in_(companies))
    rows = (await session.execute(query)).all()
    best: dict[uuid.UUID, tuple[float, int, int]] = {}
    for posting_id, start, length, dist in rows:
        if posting_id not in best or dist < best[posting_id][0]:
            best[posting_id] = (dist, start, length)
    ranked = sorted(best.items(), key=lambda item: (item[1][0], str(item[0])))
    every = among is not None or len(rows) < limit
    return [(posting_id, start, length) for posting_id, (_, start, length) in ranked], every


#: How much wider each look at the nearest chunks is than the one before.
_WIDEN_BY = 4


@stage("chunks")
async def _nearest_kept(
    session: AsyncSession,
    model: str,
    vector: list[float],
    *,
    companies: list[uuid.UUID] | None,
    want: int,
    counts: Callable[[_Hit], bool],
    skip: set[uuid.UUID] | frozenset[uuid.UUID] = frozenset(),
    window: int = _CHUNK_POOL,
    at_least: int = 0,
) -> tuple[list[_Hit], dict[uuid.UUID, tuple[int, int]]]:
    """Postings by their best chunk, nearest first, until `want` of them count.

    Every posting read is returned, counted or not, with the chunk it is
    nearest by. `skip` is left out before anything is read or counted, and
    `at_least` postings are read whatever they are, as a pool of that size
    was read before.

    The nearest `window` chunks first, as `_best_chunks` reads them. When
    those hold too few postings that count, the window is widened until
    enough do or every chunk has been read. Two hundred chunks of postings
    abroad otherwise leave the posting here unread, and the answer says there
    is none in the search area.
    """
    hydrated: dict[uuid.UUID, _Hit] = {}
    while True:
        best, every = await _chunk_ranking(
            session, model, vector, among=None, companies=companies, limit=window
        )
        order = [(pid, start, length) for pid, start, length in best if pid not in skip]
        read: list[_Hit] = []
        spans: dict[uuid.UUID, tuple[int, int]] = {}
        kept = 0
        for index, (posting_id, start, length) in enumerate(order):
            if posting_id not in hydrated:
                # Read a batch ahead, not one posting per query.
                ahead = order[index : index + max(want, at_least, 1)]
                batch = [pid for pid, _, _ in ahead if pid not in hydrated]
                hydrated.update(await _hits_for(session, batch))
            hit = hydrated.get(posting_id)
            if hit is None:
                continue
            read.append(hit)
            spans[posting_id] = (start, length)
            kept += int(counts(hit))
            if kept >= want and len(read) >= at_least:
                return read, spans
        # A window holding fewer than `at_least` postings is all a pool of
        # that size ever had; it is widened only for want of ones that count.
        if every or kept >= want:
            return read, spans
        window *= _WIDEN_BY


@stage("hydrate")
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


@stage("semantic")
async def _nearest_unfound(
    session: AsyncSession,
    chunk_vectors: dict[str, list[float]],
    companies: list[uuid.UUID] | None,
    *,
    in_area: bool,
    found: set[uuid.UUID],
    in_place: Callable[[_Hit], bool] | None = None,
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

    def counts(hit: _Hit) -> bool:
        return not (in_area and _outside_area(hit)) and (in_place is None or in_place(hit))

    spans: dict[uuid.UUID, tuple[int, int]] = {}
    hits: dict[uuid.UUID, _Hit] = {}
    for model, vector in chunk_vectors.items():
        # Read until `want` are in the area, not the first 200 and then the
        # area: those can all be abroad.
        read, nearest = await _nearest_kept(
            session,
            model,
            vector,
            companies=companies,
            want=want,
            counts=counts,
            skip=found,
            window=_WIDEN_CHUNKS,
        )
        for hit in read:
            spans.setdefault(hit.posting_id, nearest[hit.posting_id])
            hits.setdefault(hit.posting_id, hit)
    kept = [hits[posting_id] for posting_id in spans if counts(hits[posting_id])][:want]
    return kept, {hit.posting_id: spans[hit.posting_id] for hit in kept}


@stage("rerank")
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


@stage("semantic")
async def _semantic_candidates(
    session: AsyncSession,
    chunk_vectors: dict[str, list[float]],
    companies: list[uuid.UUID],
    area: str | None,
    in_place: Callable[[_Hit], bool] | None = None,
) -> tuple[list[_Hit], dict[uuid.UUID, tuple[int, int]], set[uuid.UUID]]:
    """Bounded neural candidates, sharing the search's company/area rules.

    `_best_chunks` also excludes closed postings and stale chunks. Lexical
    hashes cannot recover semantic paraphrases and are deliberately excluded.
    """
    model = SentenceTransformerEmbedder.name
    vector = chunk_vectors.get(model)
    if vector is None:
        return [], {}, set()
    # Each posting's verdict, asked once while reading and reused below.
    away: dict[uuid.UUID, bool] = {}

    def counts(hit: _Hit) -> bool:
        away[hit.posting_id] = bool(area) and _outside_area(hit)
        return not away[hit.posting_id] and (in_place is None or in_place(hit))

    # The area is asked while reading, so two hundred chunks abroad do not
    # leave nothing to keep.
    read, nearest = await _nearest_kept(
        session, model, vector, companies=companies, want=KEYWORD_POOL, counts=counts
    )
    kept = [hit for hit in read if counts(hit)][:KEYWORD_POOL]
    outside = {hit.posting_id for hit in read if away[hit.posting_id]}
    return kept, {hit.posting_id: nearest[hit.posting_id] for hit in kept}, outside


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


#: Keyword matches read first for their level when a question names one.
#: Wider than `KEYWORD_POOL` for the reason `_AREA_POOL` is: what is cut off
#: before it is filtered is a posting the answer says does not exist, and of
#: thirty postings that say "backend" one was junior. Read on past it, as
#: `_AREA_POOL` is, until thirty are at the level.
_LEVEL_POOL = 500


def _at_level(
    hits: list[_Hit], level: AskedLevel | None, *, place_filter: _PlaceFilter | None = None
) -> tuple[list[_Hit], LevelCount | None]:
    """`hits` whose titles are at the level asked, and how all of them stood.

    Titles that name no level are left out or kept by the owner's setting
    (`CHAT_ALLOW_UNKNOWN_SENIORITY`); `retrieve` puts the kept ones last.
    """
    if place_filter is not None:
        # Omitted-region counts concern the same level, but the level's own
        # breakdown must describe only jobs in the region that was asked for.
        place_filter.narrow(_at_level(hits, level)[0])
        hits = [hit for hit in hits if place_filter.keeps(hit)]
    if level is None:
        return hits, None
    return sift(
        hits,
        level,
        allow_unknown=get_settings().chat_allow_unknown_seniority,
        title=lambda hit: hit.title,
    )


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
    area: str | None,
    place_filter: _PlaceFilter | None,
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
        # Every posting that says it, as a role's are all read above and every
        # mention below. An offer is rare (15 of 5,204 in the owner's area),
        # the counts are of this set, and a pool of 500 cut it short: an offer
        # below the newest 500 refusals was one the answer said did not exist.
        pool = await _keyword_hits(session, terms, frequencies, companies, pool=None)
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
    outside = len(pool) - len(inside)
    if place_filter is not None:
        inside = place_filter.narrow(inside)
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
        outside_area=outside,
        place_total=len(place_filter.bay) if place_filter is not None else None,
        place_counts_complete=True,
        search_area_total=len(place_filter.inside) if place_filter is not None else None,
        remote_or_unplaced_not_listed=len(place_filter.omitted) if place_filter is not None else 0,
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


@traced
async def retrieve(
    session: AsyncSession, question: str, *, limit: int = DEFAULT_LIMIT
) -> Retrieval:
    """The open postings `question` is about, and how much of the corpus was read."""
    wants_share = bool(_PERCENTAGE_REQUEST.search(question))
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
    about_my_applications = asks_about_own_applications(question)
    aside = _APPLICATION_STATE if about_my_applications else frozenset()
    # A level is read from titles and never searched for as a word: "junior"
    # in a description is "mentor junior engineers" in a senior posting.
    level = asked_level(question)
    level_count: LevelCount | None = None
    if level is not None:
        aside = aside | level.words
        asks_about_jobs = asks_about_jobs or level.implies_postings
    frequencies, active_revision = await idf.load_active(session)
    companies, company_words, company_names, not_hiring = await _named_companies(
        session, words, frequencies, question
    )
    # Read the question's place once. State/country questions retain their
    # existing behavior; a named Bay Area city narrows to its whole region.
    place = locality_of(question)
    place_filter = _PlaceFilter() if place is Locality.BAY_AREA else None
    if place_filter is not None:
        # A filter is not a skill to search descriptions for. Keep city words
        # (San Francisco, etc.) so their existing location weight still sorts.
        question = re.sub(r"\bbay\s+area\b", " ", question, flags=re.I)
    # Company recognition sees the question exactly as before. Only search
    # subjects lose this framing phrase, never the standalone word "right".
    question = _FRAMING_PHRASES.sub(" ", question)
    subject_terms = [t for t in tokenize(question) if t not in _FRAMING_WORDS and t not in aside]
    if not (
        asks_about_jobs
        or companies
        or (not about_my_applications and await _names_a_title(session, subject_terms))
    ):
        return Retrieval(attempted=False)
    area = _search_area(place)
    allow_unknown = get_settings().chat_allow_unknown_seniority

    def in_area(hit: _Hit) -> bool:
        return not (area and _outside_area(hit))

    def in_place(hit: _Hit) -> bool:
        return place_filter is None or place_filter.keeps(hit)

    def passes(hit: _Hit) -> bool:
        """Whether the area and the level asked would both keep it.

        Asked while a pool is read, so they cut what was read and never what
        was not: thirty postings abroad, or five hundred at another level,
        otherwise fill a pool and the answer says there is none.
        """
        if not in_area(hit) or not in_place(hit):
            return False
        if level is None:
            return True
        place = standing(hit.title, level)
        return place < 2 or (allow_unknown and place == 2)

    filtering = area is not None or level is not None or place_filter is not None
    # Asked for by its whole title, whatever its words are: found before the
    # check below, so a posting titled "Open Application" is still a subject.
    titled = await _titles_in_question(
        session, question, companies, counts=passes if filtering else None
    )
    place_ids = {hit.posting_id for hit in titled} if place_filter is not None else set()
    only_place = place_filter is not None and not (subject_terms or companies or level)
    # A level alone names something: "internships" is every intern posting.
    if not (subject_terms or companies or titled or level is not None or only_place):
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
            area=area,
            place_filter=place_filter,
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

    outside = 0
    # By id, because one posting can be found two ways (its role and its exact
    # title) and is still one posting the area left out.
    left_out: set[uuid.UUID] = set()
    role_pool: list[_Hit] | None = None
    kinds: list[tuple[str, int]] = []
    label_of: dict[uuid.UUID, str] = {}
    title_words: tuple[str, ...] = ()
    # The tier of each posting in a list that asked for more than a role or a
    # word (`tiers.py`), and what else a role question asked, as typed.
    tier_of: dict[uuid.UUID, int] = {}
    beyond: list[str] = []
    related_total = 0
    if asked_roles:
        of_the_role = await _role_hits(session, set(asked_roles), companies)
        role_pool = [hit for hit in of_the_role if not (area and _outside_area(hit))]
        left_out |= {hit.posting_id for hit in of_the_role} - {hit.posting_id for hit in role_pool}
        role_pool, level_count = _at_level(role_pool, level, place_filter=place_filter)
        label_of = _kind_labels(role_pool)
        typed_kinds = {alias_label(alias) for alias in typed_aliases}
        kinds = _kinds(label_of, lambda label: int(label in typed_kinds), set(label_of.values()))
        # Every word, however common: "AI" is not searched for in descriptions,
        # and is still what "software engineer roles in AI" asks for.
        beyond = [
            word
            for word in dict.fromkeys(tokenize(question))
            if word not in _FRAMING_WORDS
            and word not in role_words
            and word not in company_words
            and word not in aside
        ]
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
        if stems:
            of_the_title = await _title_word_hits(session, stems, companies)
        elif level is not None:
            # Nothing but a level was asked for: "internships", "any senior
            # roles". Every title at that level is the answer, and a title
            # naming no level is not one of them whatever the setting says:
            # with nothing else to narrow by, that is half the corpus.
            asked = level
            of_the_title = await _title_word_hits(
                session, set(), companies, keep=lambda title: at_the_level(title, asked)
            )
        else:
            of_the_title = []
        if of_the_title:
            role_pool = [hit for hit in of_the_title if not (area and _outside_area(hit))]
            left_out |= {hit.posting_id for hit in of_the_title} - {
                hit.posting_id for hit in role_pool
            }
            role_pool, level_count = _at_level(role_pool, level, place_filter=place_filter)
            title_words = tuple(_as_typed(question, word) for word in asked_words)
            label_of = _kind_labels(role_pool)
            kinds = _kinds(
                label_of,
                lambda label: len(stems & {_stem(word) for word in tokenize(label)}),
                {kind for hit in role_pool if (kind := _kind_of(hit.title or "")) is not None},
            )
            related_pool = await _related_titles(
                session, frozenset(stems), companies, found=of_the_title, area=area, level=level
            )
            if place_filter is not None:
                related_pool = place_filter.narrow(related_pool)
            if related_pool:
                related_labels = {
                    posting_id: f"{tiers.RELATED_PREFIX}{label}"
                    for posting_id, label in _kind_labels(related_pool).items()
                }
                kinds = kinds + _kinds(related_labels, lambda _: 0, set(related_labels.values()))
                label_of = {**label_of, **related_labels}
                tier_of = {
                    **{hit.posting_id: tiers.IN_TITLE for hit in role_pool},
                    **{hit.posting_id: tiers.REST for hit in related_pool},
                }
                related_total = len(related_pool)
                role_pool = role_pool + related_pool
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
    count_simple = role_pool is None and (
        only_names_a_company or (not companies and len(terms) == 1)
    )
    matching_ids: list[uuid.UUID] | None = None
    # Loading the model blocks for seconds the first time, so off the loop.
    reranker = None if only_names_a_company else await asyncio.to_thread(get_reranker)
    reranked_by: str | None = None
    more_hits: list[_Hit] = []
    if terms or titled or role_pool is not None or only_place:
        pool = _AREA_POOL if area else KEYWORD_POOL
        if level is not None:
            pool = max(pool, _LEVEL_POOL)
        if role_pool is None and filtering:
            # At least `pool`, as before, and on until thirty are kept. At the
            # level asked by title, not only kept: a title naming no level is
            # listed after them (below), so it cannot stand in for one.
            asked_for = level
            set_apart = {hit.posting_id for hit in titled}

            def counts(hit: _Hit) -> bool:
                return (
                    hit.posting_id not in set_apart
                    and in_area(hit)
                    and in_place(hit)
                    and (asked_for is None or at_the_level(hit.title, asked_for))
                )

            ranking = (
                list(
                    (
                        await session.scalars(
                            select(Posting.id)
                            .where(Posting.closed_at.is_(None))
                            .order_by(Posting.first_seen_at.desc(), Posting.id)
                        )
                    ).all()
                )
                if only_place
                else await _keyword_ranking(session, terms, frequencies, companies, limit=None)
            )
            if count_simple or place_filter is not None:
                matching_ids = ranking
            scanned = await _read_until(
                session,
                ranking,
                first=pool,
                counts=counts,
                want=KEYWORD_POOL,
            )
        elif role_pool is None:
            if count_simple:
                matching_ids = await _keyword_ranking(
                    session, terms, frequencies, companies, limit=None
                )
                scanned = await _read_until(session, matching_ids[:pool], first=pool)
            else:
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
            # Never only the ones that say it: they are listed first, then
            # the rest of the role (`tiers.py`). In the keyword scan's order,
            # best match first, since that order is one of the ranks fused
            # below: the role's own order is newest first, and fusing that
            # put "software engineer jobs in London"'s first relevant posting
            # third in the owner's grades, from first.
            said = {hit.posting_id for hit in saying_it}
            scanned = saying_it + [hit for hit in role_pool if hit.posting_id not in said]
            if beyond:
                tier_of = tiers.assign(
                    {hit.posting_id: _title_stems(hit.title or "") for hit in role_pool},
                    frozenset(_stem(word) for word in beyond),
                    (hit.posting_id for hit in saying_it),
                )
            if beyond and tiers.matched(tier_of):
                # A title that is the role's own name is in every question
                # about the role, so it goes first whatever else was asked:
                # "software engineer jobs in London" opened with three
                # postings titled "Software Engineer" in San Jose that say
                # nothing of London. When something more was asked and some
                # of the role's postings say it, a posting goes first for its
                # title only if it is one of them.
                titled = [hit for hit in titled if tier_of.get(hit.posting_id) != tiers.REST]
                base_of = label_of
                label_of = tiers.relabel(
                    base_of,
                    tier_of,
                    in_title=" ".join(_as_typed(question, word) for word in beyond),
                    mentioned=", ".join(_as_typed(question, term) for term in terms),
                )
                typed_label = {label_of[i]: base_of[i] for i in label_of}
                kinds = tiers.order_kinds(
                    _kinds(
                        label_of,
                        lambda label: int(typed_label[label] in typed_kinds),
                        set(label_of.values()),
                    ),
                    label_of,
                    tier_of,
                )
                kind_rank = {kind: rank for rank, (kind, _) in enumerate(kinds)}
            elif beyond:
                tier_of = {}
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
            titled, scanned = kept_titled, kept_scanned
        if place_filter is not None:
            titled = [hit for hit in titled if in_place(hit)]
            scanned = [hit for hit in scanned if in_place(hit)]
        if level is not None:
            # After the area and before the cut. A posting asked for by its
            # whole title is held to it too: "Data Engineer" is the whole of a
            # title inside "junior data engineer jobs", and names no level.
            if role_pool is None:
                at_level, level_count = _at_level(titled + scanned, level)
                kept_ids = {hit.posting_id for hit in at_level}
                scanned = [hit for hit in scanned if hit.posting_id in kept_ids]
            else:
                kept_ids = {hit.posting_id for hit in role_pool}
            titled = [hit for hit in titled if hit.posting_id in kept_ids]
        if role_pool is None and filtering:
            # A role's postings are all listed, so they are not cut to a pool.
            if level is not None:
                # The level asked ahead of no level, before the cut and not
                # only after it: thirty postings naming no level are otherwise
                # what is kept, and the junior one is thirty-first.
                asked = level
                scanned = sorted(scanned, key=lambda hit: not at_the_level(hit.title, asked))
            scanned = scanned[:KEYWORD_POOL]
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
                session, semantic_vectors, companies, area, in_place=in_place
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
        there = {
            hit.posting_id: (
                sum(_located_at(hit, [term]) for term in terms)
                if place_filter is not None
                else int(_located_at(hit, terms))
            )
            for hit in titled
        }
        ordered = sorted(
            keyword,
            key=lambda hit: (
                -by_title.get(hit.posting_id, 0),
                -there.get(hit.posting_id, 0),
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
                session,
                chunk_vectors,
                companies,
                in_area=bool(area),
                found=set(keyword_ids),
                in_place=in_place,
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
        everything = named + rest
        if level is not None:
            # Last, and stable: the word typed, then its rung, then the titles
            # naming no level when the owner has them listed. Within each the
            # order above stands.
            asked = level
            # A role's or a title search's postings have no order of their own
            # but kind and age, so the word typed leads its rung there. What a
            # keyword found is ordered by what it says, and keeps that order.
            everything = sorted(
                everything,
                key=lambda hit: (
                    standing(hit.title, asked)
                    if role_pool is not None
                    else int(not at_the_level(hit.title, asked))
                ),
            )
        if tier_of:
            # Ordered by tier, and the model handed the first tiers only: an
            # answer must not cite a posting for a word it does not say.
            by_id = {hit.posting_id: hit for hit in everything}
            shown_ids, more_ids = tiers.split(
                [hit.posting_id for hit in everything], tier_of, limit, MORE_LIMIT
            )
            chosen = [by_id[posting_id] for posting_id in shown_ids]
            more_hits = [by_id[posting_id] for posting_id in more_ids]
        else:
            chosen = everything[:limit]
            more_hits = everything[limit : limit + MORE_LIMIT]
        ranked_total = len(everything)
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
        # `width` from each space, as before, and on until the answer's
        # `limit` are ones the area and the level keep: forty postings abroad
        # are otherwise an answer of none.
        for model, vector in chunk_vectors.items():
            read, spans = await _nearest_kept(
                session,
                model,
                vector,
                companies=companies,
                want=limit,
                counts=passes,
                at_least=width,
            )
            chunk_spans.update(spans)
            per_space.append(read)
        per_space += [
            await _nearest(session, space, vector, width, companies, counts=passes, want=limit)
            for space, vector in vectors.items()
        ]
        # Rank 1 of every space before rank 2 of any: distances from different
        # models share no scale, and ranks are all they have in common. A
        # posting in both a chunk space and a posting space is taken once.
        ranked, taken = [], set()
        for rank in range(max((len(hits) for hits in per_space), default=0)):
            for hits in per_space:
                if rank < len(hits) and hits[rank].posting_id not in taken:
                    taken.add(hits[rank].posting_id)
                    ranked.append(hits[rank])
        ranked, level_count = _at_level(ranked, level)
        reranked_total = 0
        if reranker is None:
            chosen = []
            for hit in ranked:
                if len(chosen) == limit:
                    break
                if area and _outside_area(hit):
                    outside += 1
                    continue
                if not in_place(hit):
                    continue
                chosen.append(hit)
        else:
            # The area first, so the re-ranker never spends a place on a
            # posting that would be left out anyway.
            eligible = [hit for hit in ranked if in_area(hit) and in_place(hit)]
            outside += sum(1 for hit in ranked if not in_area(hit))
            eligible, reranked_by = await _reranked(reranker, question, eligible, chunk_spans)
            chosen = eligible[:limit]
            reranked_total = min(_rerank_pool(), len(eligible)) if reranked_by else 0
        shown = {hit.posting_id for hit in chosen}
        in_area_rest = [
            hit for hit in ranked if hit.posting_id not in shown and in_area(hit) and in_place(hit)
        ]
        more_hits = in_area_rest[:MORE_LIMIT]
        ranked_total = len(chosen) + len(in_area_rest)
        searched = await _reachable(session, list(chunk_vectors), list(vectors))
        unsearchable = max(0, open_total - searched)
        if place_filter is not None:
            place_filter.narrow([hit for hit in ranked if in_area(hit)])

    match_total = company_total = None
    matched_term = None
    if count_simple or (
        place_filter is not None and role_pool is None and matching_ids is not None
    ):
        if count_simple:
            matched_term = None if only_names_a_company else _as_typed(question, terms[0])
        if only_names_a_company:
            # A company filter needs no text scan. Also covers a registry
            # name composed entirely of framing words, with no keyword IDs.
            matching_ids = list(
                (
                    await session.scalars(
                        select(Posting.id).where(
                            Posting.closed_at.is_(None), Posting.company_id.in_(companies)
                        )
                    )
                ).all()
            )
        match_total, company_total, outside = await _matching_totals(
            session,
            list(dict.fromkeys([*(matching_ids or []), *place_ids])),
            area=bool(area),
            level=level,
            allow_unknown=allow_unknown,
            place_filter=place_filter,
        )
        if not count_simple:
            match_total = company_total = None
    share = None
    if wants_share:
        # A role/title pool is complete; a keyword pool is not. The exact simple
        # search total is usable too, but never a role's unfiltered fallback.
        exact = (
            len(role_pool) - related_total
            if role_pool is not None and not beyond
            else match_total
            if role_pool is None
            else None
        )
        if exact is not None:
            denominator, _, _ = await _matching_totals(
                session,
                None,
                area=bool(area),
                level=None,
                allow_unknown=True,
                # Independent accounting: do not alter the query's omitted counts.
                place_filter=_PlaceFilter() if place_filter is not None else None,
            )
            share = PostingShare(
                matching=exact,
                open_total=denominator,
                scope=(
                    "the Bay Area"
                    if place_filter is not None
                    else "your search area"
                    if area
                    else "the full open-posting corpus"
                ),
            )
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
        match_total=match_total,
        company_total=company_total,
        matched_term=matched_term,
        share=share,
        share_requested=wants_share,
        not_hiring=tuple(sorted(not_hiring)),
        area=area,
        area_waived=area is None and get_settings().search_us_only,
        outside_area=outside,
        place_total=len(place_filter.bay) if place_filter is not None else None,
        place_counts_complete=role_pool is not None or matching_ids is not None,
        search_area_total=len(place_filter.inside) if place_filter is not None else None,
        remote_or_unplaced_not_listed=len(place_filter.omitted) if place_filter is not None else 0,
        reranked_by=reranked_by,
        ranked=ranked_total,
        reranked=reranked_total,
        roles=tuple(display_name(key) for key in sorted(asked_roles)),
        role_keys=tuple(sorted(asked_roles)),
        role_total=len(role_pool) if role_pool is not None and asked_roles else None,
        title_words=title_words,
        title_total=(
            len(role_pool) - related_total if role_pool is not None and title_words else None
        ),
        related_total=related_total,
        beyond_role=tuple(_as_typed(question, word) for word in beyond),
        beyond_in_title=sum(1 for tier in tier_of.values() if tier == tiers.IN_TITLE)
        if beyond
        else 0,
        beyond_mentioned=(
            sum(1 for tier in tier_of.values() if tier == tiers.MENTIONED)
            if beyond and terms
            else None
        ),
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
        level=level_count,
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
