"""How many years a posting demands, and how firmly — read from real postings.

`docs/BACKLOG.md` P7. `Posting` records no experience requirement, so a role
asking for ten years reaches a new graduate's feed ranked by cosine alone, and
a cosine cannot refuse a seniority demand.

**The positives in here are verbatim from `tests/fixtures/golden/postings.json`**
— twelve postings crawled from real boards. That matters because §15 records
the opposite practice costing this repo twice: Gate 6's rejection patterns were
written beside the fixtures that exercise them and matched "with other
candidates" while missing "with another candidate", and a Greenhouse fixture
had native `<select>` while the live board had moved to react-select. A pattern
and its test case written together prove only that they agree.

What the corpus cannot prove is recorded at the bottom: none of the twelve
postings puts a years line under a nice-to-have heading, so the *preferred*
path is exercised by constructed placement — real heading wording, from the
same corpus, around a years line that did not sit there.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from packages.core.models import Posting, Profile
from packages.matching.experience import Demand, read_posting
from packages.matching.filters import apply_filters, experience_ok
from packages.matching.rubric import _experience

GOLDEN = Path(__file__).parent / "fixtures" / "golden" / "postings.json"


def _golden() -> dict[str, str]:
    rows = json.loads(GOLDEN.read_text(encoding="utf-8"))["postings"]
    return {row["title"]: row.get("description") or "" for row in rows}


def _posting(description: str) -> Posting:
    return Posting(
        id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        url=f"https://example.com/{uuid.uuid4()}",
        title="Backend Engineer",
        location="San Francisco, CA",
        description_raw=description,
    )


def _profile(**kwargs: object) -> Profile:
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "candidate_id": uuid.uuid4(),
        "label": "default",
        "location": "San Francisco, CA",
        "links_json": {},
        "answers_kv_json": {},
    }
    defaults.update(kwargs)
    return Profile(**defaults)


# --------------------------------------------------------------------------
# Read from the crawled corpus
# --------------------------------------------------------------------------

#: title -> the mandatory minimum the posting states. The values were read off
#: the postings by hand; the ranges are the interesting ones, because a range's
#: *lower* bound is the requirement and reading `8–12+ years` as twelve would
#: exclude a posting that said eight.
CORPUS_DEMANDS = {
    "Partner Solutions Architect (ANZ)": 10,  # "typically have 10-15 years"
    "Sr. Manager, Field Engineering (Specialist) - HCLS": 5,  # two 5+ lines
    "Forward Deployed Engineer": 5,
    "Enterprise Account Executive, Growth": 3,  # "3+ years field experience"
    "Senior Revenue Accountant": 3,  # "3+ years’ experience", curly quote
    "Product Designer, Growth": 8,  # "8–12+ years", en dash
    "Strategic Finance Manager, GTM": 2,  # 8-10 and 2+, the smaller binds
}

#: Postings in the same corpus that state no years requirement at all. Two of
#: them mention a year in prose — "A Year at Palantir", "Come join us for a
#: year" — which is exactly the kind of phrase a digit-hungry pattern eats.
CORPUS_SILENT = {
    "Forward Deployed Software Engineer",
    "Year at Palantir - Forward Deployed Software Engineer, Internship - Commercial",
    "Deployment Strategist",
    "Data Scientist, Pricing",
    "Product Engineer",
}


@pytest.mark.parametrize(("title", "years"), sorted(CORPUS_DEMANDS.items()))
def test_a_real_posting_states_the_years_it_needs(title: str, years: int) -> None:
    read = read_posting(_golden()[title])
    assert read.mandatory_minimum == years, read.summary()


@pytest.mark.parametrize("title", sorted(CORPUS_SILENT))
def test_a_real_posting_that_says_nothing_yields_nothing(title: str) -> None:
    read = read_posting(_golden()[title])
    assert not read.stated
    assert read.summary() is None, "a silence must not be rendered as a finding"


def test_every_corpus_demand_is_classified_one_way_or_the_other() -> None:
    """No posting in the corpus leaves a years line unattributed.

    This is the measurement that made the heading allowlist worth having: with
    only the obvious headings, three of the seven read as `AMBIGUOUS` — which
    never excludes, so the filter would have looked armed and done nothing on
    `What We Look For`, `What you bring to the table` and `Your background
    looks something like this`.
    """
    ambiguous = {
        title: [
            d.evidence.quote for d in read_posting(text).demands if d.demand is Demand.AMBIGUOUS
        ]
        for title, text in _golden().items()
    }
    assert not {t: q for t, q in ambiguous.items() if q}


# --------------------------------------------------------------------------
# Durations that are not requirements
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "Datadog was founded 10 years ago in New York.",
        "Over the past 5 years we have grown from 20 to 200 people.",
        "We expect to double headcount within 2 years.",
        "Equipment is refreshed every 3 years.",
        "A 4 year degree is not required for this role.",
        "You should be able to commit to 2 years in the role.",
        "Come join us for a year and build something that matters.",
    ],
)
def test_a_duration_in_prose_is_not_a_requirement(line: str) -> None:
    assert not read_posting(line).stated


def test_the_attachment_window_does_not_reach_the_next_bullet() -> None:
    """A duration on one line cannot borrow the next line's "experience".

    Lines are read one at a time for this reason: joined into a paragraph, the
    noun from a following bullet would rescue a duration that is not a demand.
    """
    text = "We were founded 10 years ago.\nStrong experience with Python and Postgres."
    assert not read_posting(text).stated


# --------------------------------------------------------------------------
# How firm the demand is
# --------------------------------------------------------------------------


#: Heading wording taken from the corpus. `things we love` is MongoDB's
#: nice-to-have list, `bonus points` is Datadog's, and Palantir pairs `What We
#: Value` with `What We Require`.
@pytest.mark.parametrize(
    "heading", ["Nice to have", "Bonus Points:", "Things we love", "What We Value"]
)
def test_a_years_line_under_a_nice_to_have_heading_is_preferred(heading: str) -> None:
    read = read_posting(f"{heading}\n5+ years of experience with Kubernetes")
    assert read.mandatory_minimum is None, "a preferred demand must not read as mandatory"
    assert read.preferred_minimum == 5


@pytest.mark.parametrize(
    "heading",
    [
        "Requirements",
        "Minimum qualifications",
        "What We Require",
        "Who You Are:",
        "What we look for",
    ],
)
def test_a_years_line_under_a_requirements_heading_is_mandatory(heading: str) -> None:
    assert read_posting(f"{heading}\n5+ years of experience with Kubernetes").mandatory_minimum == 5


def test_the_line_outranks_the_heading_in_both_directions() -> None:
    under_required = read_posting("Requirements\n8+ years of experience preferred")
    assert under_required.mandatory_minimum is None
    assert under_required.preferred_minimum == 8

    under_preferred = read_posting("Nice to have\nMinimum 8 years of experience")
    assert under_preferred.mandatory_minimum == 8


def test_an_unrecognised_heading_clears_the_previous_one() -> None:
    """The reset is load-bearing, not tidiness.

    On a real posting the requirements heading is several screens above the
    benefits section. Without the reset, a years line under "Benefits" or "Pay
    Range Transparency" — both real headings from the corpus — inherits
    `MANDATORY` from a list it has nothing to do with, and the filter excludes
    on a perk.
    """
    read = read_posting(
        "Requirements\n5+ years of experience with Python\n"
        "Pay Range Transparency\nSabbatical after 7 years of experience here"
    )
    assert read.mandatory_minimum == 5
    assert [d.demand for d in read.demands] == [Demand.MANDATORY, Demand.AMBIGUOUS]


def test_the_smallest_mandatory_demand_is_the_binding_one() -> None:
    """Two routes in, and this module cannot tell a conjunction from an "or".

    Keeping the posting and showing both demands is the error worth making:
    hiding a job the owner might hold is invisible, and §15 records that
    asymmetry deciding the same question for `locality.py`.
    """
    read = read_posting("Requirements\n8-10 years of experience in finance\n2+ years of banking")
    assert read.mandatory_minimum == 2
    assert read.summary() == "requires 2+ years; requires 8+ years"


# --------------------------------------------------------------------------
# The filter
# --------------------------------------------------------------------------


def test_no_bound_filters_nothing() -> None:
    posting = _posting("Requirements\n15+ years of experience required")
    assert experience_ok(posting, None)


def test_a_mandatory_demand_above_the_bound_excludes() -> None:
    posting = _posting("Requirements\n10+ years of experience with Python")
    assert not experience_ok(posting, 3)
    assert experience_ok(posting, 10), "the bound is inclusive"


def test_a_preferred_demand_above_the_bound_never_excludes() -> None:
    """The posting said the requirement was optional; a filter must not overrule it."""
    posting = _posting("Nice to have\n10+ years of experience with Python")
    assert experience_ok(posting, 3)


def test_an_ambiguous_demand_above_the_bound_never_excludes() -> None:
    """Same rule as `eligibility.py`: a hard filter needs evidence."""
    posting = _posting("10+ years of experience with Python")
    assert experience_ok(posting, 3)


def test_a_bound_of_zero_is_a_real_answer() -> None:
    """ "Only roles that ask for no experience" is what a new graduate wants.

    It is also why the dashboard cannot send this field through the empty-string
    mapping the text fields use: `Number("")` is 0, so a blank box would become
    the strictest filter there is.
    """
    assert not experience_ok(_posting("Requirements\n1+ years of experience"), 0)
    assert experience_ok(_posting("We will train you."), 0)


def test_the_bound_falls_back_to_the_profile() -> None:
    """`apply_filters` reads the column, so every caller of the hard filters gets it.

    The same arrangement as `target_seniority`, and for the reason §15 records:
    until the fallback existed no production path set a target, so the rung
    filter was reachable only from the benchmark.
    """
    posting = _posting("Requirements\n12+ years of experience leading teams")
    assert apply_filters(_profile(), posting).passed
    verdict = apply_filters(_profile(max_required_experience_years=4), posting)
    assert not verdict.passed
    assert verdict.reasons == ["posting requires 12+ years of experience, above the 4-year limit"]


# --------------------------------------------------------------------------
# The rubric — where a preferred demand has its effect
# --------------------------------------------------------------------------


def test_nothing_stated_carries_no_weight() -> None:
    dimension = _experience(_posting("Build services."), 3)
    assert dimension.weight == 0.0
    assert "does not state" in dimension.finding


def test_no_bound_carries_no_weight_but_still_reports() -> None:
    dimension = _experience(_posting("Requirements\n9+ years of experience"), None)
    assert dimension.weight == 0.0
    assert "requires 9+ years" in dimension.finding


def test_a_preferred_demand_over_the_bound_lowers_the_dimension() -> None:
    """This is the "only lowers" half of P7, and it is deliberately not in the score.

    `rubric.py` explains the ranking rather than producing it, so a preferred
    demand the owner is under costs a visible dimension and not a place in the
    feed. Tuning the cosine here would invalidate Gate 5 and hide the reason.
    """
    dimension = _experience(_posting("Nice to have\n9+ years of experience"), 3)
    assert dimension.weight == 0.15
    assert 1.0 < dimension.score < 5.0

    met = _experience(_posting("Nice to have\n2+ years of experience"), 3)
    assert met.score == 5.0


def test_a_mandatory_demand_over_the_bound_scores_one() -> None:
    assert _experience(_posting("Requirements\n9+ years of experience"), 3).score == 1.0


def test_an_ambiguous_demand_is_reported_without_being_scored() -> None:
    dimension = _experience(_posting("9+ years of experience"), 3)
    assert dimension.weight == 0.0
    assert "9+ years" in dimension.finding


# --------------------------------------------------------------------------
# A filter with no control can never fire
# --------------------------------------------------------------------------

WEB = Path(__file__).parent.parent / "apps" / "web" / "src" / "app" / "profile"


def test_both_search_filters_have_a_control_on_the_profile_page() -> None:
    """§15 records this defect twice, once for each of these two fields.

    `citizenship_status` shipped with a column, a schema, a filter and tests,
    and no input — so the filter could never fire on real data. `target_seniority`
    shipped the same way, with a measured payoff (P@10 0.900 to 1.000) and no
    way to set it outside curl. Both are on the page now, and this asserts it
    rather than trusting that anyone will notice.
    """
    form = (WEB / "profile-form.tsx").read_text(encoding="utf-8")
    assert 'name="target_seniority"' in form
    assert 'name="max_required_experience_years"' in form

    action = (WEB / "actions.ts").read_text(encoding="utf-8")
    assert "target_seniority" in action
    assert "max_required_experience_years" in action


def test_the_api_accepts_the_bound_and_refuses_an_absurd_one() -> None:
    from pydantic import ValidationError

    from packages.core.schemas import ProfileUpdate

    assert ProfileUpdate(max_required_experience_years=0).max_required_experience_years == 0
    assert ProfileUpdate().max_required_experience_years is None
    with pytest.raises(ValidationError):
        ProfileUpdate(max_required_experience_years=99)


def test_the_patch_route_can_reach_the_column() -> None:
    """A field the update map does not name is silently dropped by the route."""
    from apps.api.routers.profiles import _UPDATE_FIELDS

    assert _UPDATE_FIELDS["max_required_experience_years"] == "max_required_experience_years"
