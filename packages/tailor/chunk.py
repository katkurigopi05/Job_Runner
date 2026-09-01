"""Noun-phrase extraction, so the guard checks what §9 says it checks.

CLAUDE.md §9 Gate 3: "every **noun-phrase** entity in output traces to the
source résumé". `guard.py` approximated noun phrases with capitalization,
acronyms and digits, and that proxy has a hole a résumé-sized lie fits
through:

    résumé:  "Maintained the billing service and reduced invoice errors."
    rewrite: "...using a message queue ... with machine learning."
    -> accepted, 0 entities checked

Nothing there is capitalized, numeric or an acronym, so nothing was checked.
"machine learning" is precisely the kind of claim §2.1 forbids adding.

## Why part of speech, and not just "unknown word"

Flagging any unfamiliar lowercase word would break the latitude §2.1 grants:
a rewrite saying "oversaw" where the source says "maintained" is rephrasing,
not invention, and rejecting it would make the tailorer look broken. Verbs and
adverbs are free. Nouns and the adjectives attached to them are claims. That
distinction needs a tagger, which is the whole reason the original used
capitalization instead.

## Degrading without lying about it

The tagger needs NLTK data that is downloaded, not shipped. On a machine
without it, this reports `available = False` and the caller falls back to the
capitalization heuristic — the guard gets weaker, and the weakening is
recorded on `GuardReport.extractor` rather than being silent. `make doctor`
checks for it. A guard that quietly loses a check is worse than one that
never had it, because nobody re-reads a green test.
"""

from __future__ import annotations

import functools
from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: Adjectives and nouns run together, ending on a noun. Deliberately does not
#: cross a preposition: "engineer at Google" is two phrases, and treating it
#: as one would make the guard demand the whole span appear verbatim.
NP_GRAMMAR = r"NP: {<JJ.*|NN.*>*<NN.*>}"

#: Tags whose words carry a factual claim. Verbs, adverbs, determiners and
#: prepositions are how a sentence is phrased, not what it asserts.
CLAIM_TAGS = ("NN", "JJ")


@functools.lru_cache(maxsize=1)
def _parser() -> Any | None:
    """The chunker, or None when the tagger data is not installed."""
    try:
        import nltk

        # Both are needed and neither ships with the package. Touching them
        # here means the failure surfaces once, at import, rather than as a
        # LookupError in the middle of an application.
        nltk.data.find("taggers/averaged_perceptron_tagger_eng")
        nltk.word_tokenize("probe sentence")
        return nltk.RegexpParser(NP_GRAMMAR)
    except Exception as exc:  # noqa: BLE001 - any failure means fall back
        log.info("np_chunker_unavailable", error=type(exc).__name__)
        return None


def _opening_word(tokens: list[str]) -> str | None:
    """The first real word of a bullet, which the noun pass skips.

    Sentence-initial position is where a POS tagger is least reliable — the
    capitalization that normally marks a proper noun is there for every
    sentence — and a résumé bullet opens with a verb almost by convention.
    Measured across 22 common openers, "- Built and tested the billing
    service" mis-tagged the verb as a noun 7 times out of 22; the simple form
    only once.

    Skipping it costs a lowercase noun that opens a bullet, which is rare.
    A capitalized one is still caught by the capitalization pass, so nothing
    like "Kubernetes migration reduced errors" slips through this.
    """
    for token in tokens:
        if token[:1].isalpha():
            return token
    return None


def available() -> bool:
    """Whether noun-phrase extraction can actually run here."""
    return _parser() is not None


def claim_words(text: str) -> list[str]:
    """Nouns and adjectives inside noun phrases, in order, as written.

    Case and spelling are preserved because these become `Entity.text`, which
    is what a violation shows the owner. Reporting `spark` for a résumé that
    says `Spark` makes the finding look like it is about something else.

    Empty when the tagger is unavailable — the caller checks `available()`
    rather than reading an empty list as "nothing to check".
    """
    parser = _parser()
    if parser is None or not text.strip():
        return []

    import nltk

    words: list[str] = []
    for sentence in nltk.sent_tokenize(text):
        tokens = nltk.word_tokenize(sentence)
        tagged = nltk.pos_tag(tokens)
        opener = _opening_word(tokens)

        for subtree in parser.parse(tagged).subtrees(lambda t: t.label() == "NP"):
            for word, tag in subtree.leaves():
                if not tag.startswith(CLAIM_TAGS):
                    continue
                if not any(character.isalpha() for character in word):
                    # "%", "$", bare figures. The tagger calls these nouns;
                    # the number pass already owns them, and the corpus index
                    # strips the symbol off "40%" so a lookup here never
                    # matches anything.
                    continue
                if opener is not None and word == opener:
                    # See _opening_word: the tagger is least reliable here and
                    # a résumé bullet opens with a verb.
                    opener = None
                    continue
                stripped = word.strip(".,;:()[]\"'")
                if stripped:
                    words.append(stripped)
    return words


def noun_phrases(text: str) -> list[str]:
    """Whole noun phrases, for reporting a violation in the owner's terms.

    A finding that says "message queue" is one a person can act on; one that
    says "queue" makes them go looking for what it meant.
    """
    parser = _parser()
    if parser is None or not text.strip():
        return []

    import nltk

    phrases: list[str] = []
    for sentence in nltk.sent_tokenize(text):
        tagged = nltk.pos_tag(nltk.word_tokenize(sentence))
        for subtree in parser.parse(tagged).subtrees(lambda t: t.label() == "NP"):
            phrases.append(" ".join(word for word, _ in subtree.leaves()))
    return phrases
