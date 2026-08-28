"""Terms that mean the same thing, so a résumé is not judged on spelling.

A posting asks for "PostgreSQL"; the résumé says "Postgres". A posting asks for
"Kubernetes"; the résumé says "K8s". The same fact, written two ways — and
until this existed the second one counted for nothing: `keywords.analyze`
reported the term unsupported, told the model never to use it, and the guard
would have refused it as a fabrication if the model had anyway.

The effect was a tailorer that could not use the posting's own vocabulary for
skills the owner demonstrably has. On a real résumé — Postgres, K8s, ETL, REST
APIs, ML — a posting naming all five by their long forms matched **none** of
them.

## Why this is not a hole in §2.1

§2.1 forbids adding a skill the résumé does not support. An alias adds no
skill. "Postgres" and "PostgreSQL" are the same product, so a résumé asserting
one asserts the other; the claim is identical and only the surface form
changes. That is squarely inside "rephrase, reorder, re-emphasize".

What would be a hole is a table of *related* terms. Two rules keep it out:

- **Only true equivalences.** Abbreviations, expansions, vendor spellings,
  and exact synonyms. Never "adjacent", "implies", or "usually comes with".
  `Java` and `JavaScript` are not aliases. `ML` and `AI` are not aliases.
  `React` and `frontend` are not aliases — one is a library, the other a
  discipline, and a résumé that shows React does not thereby show every
  frontend skill a posting might list.
- **Symmetric and lossless.** Every member of a group must be substitutable
  for every other in a factual claim. If substituting one for another could
  make a sentence false, they do not belong in the same group.

Adding a group widens what the guard permits, so it is a §2.1 decision and not
a data-entry chore. `SourceCorpus` says the same thing about its own inputs:
"widening this set is how a fabrication becomes permissible, so it should be
done deliberately and never quietly."
"""

from __future__ import annotations

#: Groups of interchangeable surface forms, lowercase.
#:
#: Multi-word members are matched as phrases against the normalized source
#: text; single words are matched as tokens. Keep both spellings of anything
#: commonly written either way.
#:
#: Deliberately absent, and each for a reason worth keeping written down:
#:
#: - `java` / `javascript` — different languages that share four letters.
#: - `ml` / `ai` — one is a field, the other a superset that includes work
#:   the résumé may not have done.
#: - `mysql` / `mariadb`, `docker` / `containers`, `git` / `version control`
#:   — a fork, an implementation, and a tool. Each is *an* example of the
#:   other, not the same thing, and a résumé showing one does not show the
#:   other.
#: - `terraform` / `tf`, `typescript` / `ts`, `python` / `py` — the short
#:   forms collide with too much else to be safe (`tf` is also TensorFlow).
ALIAS_GROUPS: tuple[frozenset[str], ...] = tuple(
    frozenset(group)
    for group in (
        # Datastores
        {"postgres", "postgresql", "psql"},
        {"mongo", "mongodb"},
        {"elastic", "elasticsearch"},
        {"sql server", "mssql", "microsoft sql server"},
        # Platforms and infrastructure
        {"kubernetes", "k8s"},
        {"amazon web services", "aws"},
        {"google cloud platform", "google cloud", "gcp"},
        {"microsoft azure", "azure"},
        {"continuous integration", "ci"},
        {"continuous delivery", "continuous deployment", "cd"},
        {"infrastructure as code", "iac"},
        {"continuous integration and continuous delivery", "ci/cd", "cicd", "ci cd"},
        {"site reliability engineering", "sre"},
        # Data
        {"etl", "extract transform load", "data pipeline", "data pipelines"},
        {"machine learning", "ml"},
        {"natural language processing", "nlp"},
        {"large language model", "large language models", "llm", "llms"},
        {"business intelligence", "bi"},
        # Web and APIs
        {"rest", "restful", "rest api", "rest apis", "restful api", "restful apis"},
        {"web services", "web service", "http services", "http api", "http apis"},
        {"single page application", "spa"},
        {"application programming interface", "api", "apis"},
        {"software development kit", "sdk"},
        {"graphql", "graph ql"},
        # Languages and runtimes — expansions and spellings only, never siblings
        {"golang", "go lang"},
        {"c sharp", "csharp"},
        {"node", "nodejs", "node js"},
        # Practices
        {"test driven development", "tdd"},
        {"object oriented programming", "oop"},
        {"user interface", "ui"},
        {"user experience", "ux"},
        {"quality assurance", "qa"},
    )
)


#: token or phrase -> every equivalent form, itself included.
_LOOKUP: dict[str, frozenset[str]] = {}
for _group in ALIAS_GROUPS:
    for _member in _group:
        _LOOKUP[_member] = _LOOKUP.get(_member, frozenset()) | _group

#: Members that are a single word, for cheap token-level expansion.
_SINGLE_WORD: dict[str, frozenset[str]] = {
    term: forms for term, forms in _LOOKUP.items() if " " not in term
}

#: Members that are phrases, for substring matching against normalized text.
_PHRASES: tuple[tuple[str, frozenset[str]], ...] = tuple(
    (term, forms) for term, forms in _LOOKUP.items() if " " in term
)


def equivalents(term: str) -> frozenset[str]:
    """Every form `term` may also be written as, including itself.

    Empty for a term with no alias group, so callers can treat "has aliases" and
    "is its own only form" the same way.
    """
    return _LOOKUP.get(term.strip().lower(), frozenset())


def expand_tokens(tokens: set[str]) -> set[str]:
    """Single-word aliases implied by an already-normalized token set."""
    extra: set[str] = set()
    for token in tokens:
        forms = _SINGLE_WORD.get(token)
        if forms:
            # Only single-word equivalents go into a token index; a phrase is
            # not a token and would never match one.
            extra |= {form for form in forms if " " not in form}
    return extra


def expand_phrases(normalized_text: str) -> set[str]:
    """Single-word aliases implied by phrases appearing in the source text.

    "machine learning" in a résumé makes the token "ml" available, which no
    amount of per-token indexing would find — the phrase is two tokens and the
    alias is one.
    """
    if not normalized_text:
        return set()
    found: set[str] = set()
    for phrase, forms in _PHRASES:
        if phrase in normalized_text:
            found |= {form for form in forms if " " not in form}
    return found


def known_phrases() -> tuple[str, ...]:
    """Multi-word members of the table, longest first.

    Longest first so "continuous integration and continuous delivery" is
    recognized before "continuous integration" claims its words.
    """
    return tuple(sorted((term for term, _ in _PHRASES), key=len, reverse=True))
