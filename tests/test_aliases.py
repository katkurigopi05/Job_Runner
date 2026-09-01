"""Equivalent spellings of the same fact, and the line they must not cross.

A posting asks for "PostgreSQL"; the résumé says "Postgres". Before this, that
counted for nothing: `analyze` reported the term unsupported, told the model
never to use it, and the guard would have refused it as fabrication if the
model had reached for it anyway.

The effect was a tailorer unable to use the posting's own vocabulary for skills
the owner demonstrably has — which is most of what résumé tailoring *is*, and
what an ATS scans for.

The risk in fixing it is obvious: an alias table is a list of things the guard
will now permit. These tests hold both halves — the equivalences it must
recognize, and the merely-related terms it must keep refusing.
"""

from __future__ import annotations

from packages.tailor.aliases import equivalents
from packages.tailor.guard import SourceCorpus, check
from packages.tailor.keywords import analyze

RESUME = (
    "Built backend services in Python on Postgres. Deployed with K8s. "
    "Wrote ETL jobs in Go. Used ML models in production."
)


def _corpus() -> SourceCorpus:
    return SourceCorpus.from_texts(RESUME)


# --------------------------------------------------------------------------
# What it must now recognize
# --------------------------------------------------------------------------


def test_a_posting_term_is_supported_by_its_abbreviation_in_the_resume() -> None:
    """The case the table exists for."""
    report = analyze("We need PostgreSQL and Kubernetes.", _corpus())

    assert "PostgreSQL" in report.supported
    assert "Kubernetes" in report.supported


def test_a_multi_word_skill_is_one_term_not_two() -> None:
    """ "machine learning" split into "machine" and "learning" asks the wrong
    question twice: neither word is a skill, a résumé saying "ML" backs
    neither, and both landed on the off-limits list — forbidding the model from
    writing the phrase the posting cared about most."""
    report = analyze("Deep machine learning experience required.", _corpus())

    assert "machine learning" in report.supported
    assert "machine" not in report.missing
    assert "learning" not in report.missing


def test_the_guard_accepts_a_rewrite_that_uses_the_equivalent_spelling() -> None:
    """The half that makes the other half usable.

    `analyze` inviting the model to write "PostgreSQL" while the guard refuses
    it as fabrication would be worse than never offering the term: the model
    takes the invitation, the guard discards the bullet, and the owner sees a
    tailorer that keeps failing for no visible reason.
    """
    assert check("Built services on PostgreSQL.", _corpus()).ok
    assert check("Deployed with Kubernetes.", _corpus()).ok
    # Opens with a verb the source uses. A different opening verb is refused
    # for an unrelated reason — the guard reads a capitalized sentence-initial
    # verb as a proper noun — and that must not be mistaken for an alias
    # failure while this test is the thing proving aliases work.
    assert check("Used machine learning models in production.", _corpus()).ok


def test_one_term_per_group_rather_than_every_spelling() -> None:
    """ "data pipeline" and "data pipelines" are one skill written twice."""
    report = analyze("We build data pipelines and data pipeline tooling.", _corpus())

    emitted = [t for t in report.supported + report.missing if "pipeline" in t]
    assert len(emitted) == 1, emitted


# --------------------------------------------------------------------------
# The line it must not cross
# --------------------------------------------------------------------------


def test_related_is_not_equivalent() -> None:
    """The failure mode of every alias table.

    Each of these pairs is a real skill distinction. Treating them as the same
    would let the guard pass a claim the résumé does not support, which is
    exactly the §2.1 hole this table could become.
    """
    for term in ("java", "javascript", "mysql", "git", "terraform", "ai"):
        assert equivalents(term) == frozenset(), f"{term} must not have aliases"


def test_a_skill_the_resume_lacks_is_still_off_limits_and_still_refused() -> None:
    """Widening the corpus must not widen it past the résumé."""
    report = analyze("We need JavaScript and Oracle.", _corpus())

    assert "JavaScript" in report.missing
    assert "Oracle" in report.missing
    assert not check("Built the frontend in JavaScript.", _corpus()).ok
    assert not check("Ran the reporting stack on Oracle.", _corpus()).ok


def test_every_group_is_symmetric() -> None:
    """A member must be substitutable for every other member.

    If substituting one for another could make a sentence false, they do not
    belong in the same group — that is the rule the table is built on, and this
    is what stops it drifting into "usually comes with".
    """
    from packages.tailor.aliases import ALIAS_GROUPS

    for group in ALIAS_GROUPS:
        for member in group:
            assert equivalents(member) == group, f"{member} is not symmetric in {sorted(group)}"


# --------------------------------------------------------------------------
# A verb is not a name
# --------------------------------------------------------------------------


def test_an_ordinary_action_verb_is_not_a_proper_noun() -> None:
    """A résumé bullet opens with a verb, and the proper-noun test is
    capitalization — so a rewrite opening with any verb the source had not
    already used was refused as a fabricated name. "Deployed the pipeline"
    failed against a source saying "Wrote the pipeline", for the word
    "Deployed"."""
    from packages.tailor.guard import EntityKind, _classify

    for verb in (
        "Deployed",
        "Tested",
        "Automated",
        "Integrated",
        "Documented",
        "Monitored",
        "Operated",
        "Resolved",
        "Validated",
        "Rewrote",
    ):
        assert _classify(verb) != EntityKind.PROPER_NOUN, verb


def test_a_seniority_verb_is_still_a_claim() -> None:
    """The distinction the fix must not flatten.

    "Deployed" says what was done; "oversaw" says who you were while doing it.
    Claiming you led what you contributed to is a fabrication of scope, and the
    one a résumé rewrite is most tempted by.
    """
    from packages.tailor.guard import EntityKind, _classify

    for verb in ("Led", "Managed", "Owned", "Architected", "Oversaw", "Founded", "Spearheaded"):
        assert _classify(verb) == EntityKind.SCOPE, verb


def test_the_object_of_an_allowed_verb_is_still_checked() -> None:
    """Freeing the verb must not free what follows it."""
    from packages.tailor.guard import SourceCorpus, check

    corpus = SourceCorpus.from_texts("Wrote the data pipeline that feeds the service, in Go.")

    assert check("Deployed the data pipeline, in Go.", corpus).ok
    assert not check("Deployed the data pipeline to Kubernetes.", corpus).ok
    assert not check("Tested the data pipeline, cutting latency by 40%.", corpus).ok
    assert not check("Led the data pipeline work.", corpus).ok


# --------------------------------------------------------------------------
# The other direction: the output spells out what the source shortened
# --------------------------------------------------------------------------


def test_the_output_may_spell_out_what_the_source_abbreviated() -> None:
    """The mirror of `expand_phrases`, and it was missing.

    Indexing handles source-says-long / output-says-short: "machine learning"
    in a résumé makes `ml` available. The reverse never worked, because
    `expand_tokens` keeps phrases out of a token index on purpose — so a source
    saying `ML` left "machine" and "learning" tracing to nothing.

    Invisible while the guard matched on capitalization: "machine learning" is
    lowercase and carried no entity. The noun-phrase extractor reads every
    noun, so it began refusing rewrites written in the résumé's own vocabulary.
    """
    corpus = SourceCorpus.from_texts("Used ML models in production. Deployed to K8s.")

    assert check("Used machine learning models in production.", corpus).ok
    assert check("Deployed to Kubernetes.", corpus).ok


def test_spelling_out_a_term_the_source_never_had_is_still_refused() -> None:
    """The check reads the alias table, not the output's own confidence."""
    corpus = SourceCorpus.from_texts("Used ML models in production.")

    # `ci` is a real alias group; this source simply does not assert it.
    assert not check("Built continuous integration pipelines.", corpus).ok
    # Not aliases of ML at all, and deliberately so — see ALIAS_GROUPS.
    assert not check("Used deep learning models.", corpus).ok
    assert not check("Used artificial intelligence in production.", corpus).ok
