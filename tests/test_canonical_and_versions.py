"""Merging copies of one requisition, and keeping what a posting used to say.

The merge tests are mostly refusals, on purpose. A duplicate card costs a
second look; a wrong merge hides a real opening and nothing says so.
"""

from __future__ import annotations

import uuid

from packages.core.models import Posting
from packages.matching.canonical import requisition_id, same_requisition
from packages.matching.requirements import extract
from packages.matching.versions import describe_pay, diff, snapshot

COMPANY = uuid.uuid4()
BODY = (
    "We are hiring a backend engineer to build payment infrastructure. You will design "
    "APIs, operate PostgreSQL at scale, and mentor engineers on reliability practices. "
    "Requirements: Python, distributed systems experience, and on-call ownership of services."
)


def _listing(
    *,
    url: str = "https://boards.greenhouse.io/acme/jobs/1",
    ats: str = "greenhouse",
    title: str = "Backend Engineer",
    location: str = "Remote - US",
    body: str = BODY,
    company: uuid.UUID = COMPANY,
    locked: bool = False,
) -> Posting:
    return Posting(
        id=uuid.uuid4(),
        company_id=company,
        url=url,
        ats_type=ats,
        title=title,
        location=location,
        description_raw=body,
        canonical_locked=locked,
    )


CAREERS = {"url": "https://acme.example/careers/backend-engineer", "ats": "jsonld"}


def test_the_same_text_on_two_sources_is_one_requisition() -> None:
    verdict = same_requisition(_listing(), _listing(**CAREERS))

    assert verdict.same
    assert verdict.confidence >= 0.9


def test_two_listings_on_one_board_are_two_openings() -> None:
    verdict = same_requisition(_listing(), _listing(url="https://boards.greenhouse.io/acme/jobs/2"))

    assert not verdict.same
    assert verdict.reason == "the same source lists them separately"


def test_different_requisition_ids_are_never_merged() -> None:
    first = _listing(body=BODY + " Req ID: R-10421")
    second = _listing(body=BODY + " Req ID: R-10999", **CAREERS)

    assert not same_requisition(first, second).same


def test_a_shared_requisition_id_merges_despite_edited_text() -> None:
    first = _listing(body="Req ID: R-10421. Short.")
    second = _listing(body="Different wording entirely. Requisition #R-10421", **CAREERS)

    verdict = same_requisition(first, second)
    assert verdict.same
    assert "R-10421" in verdict.reason


def test_a_different_location_is_a_different_opening() -> None:
    assert not same_requisition(_listing(), _listing(location="London, UK", **CAREERS)).same


def test_different_companies_never_merge() -> None:
    assert not same_requisition(_listing(), _listing(company=uuid.uuid4(), **CAREERS)).same


def test_an_owner_split_is_respected() -> None:
    verdict = same_requisition(_listing(locked=True), _listing(**CAREERS))

    assert verdict.reason == "kept separate by the owner"


def test_short_descriptions_are_not_enough_evidence() -> None:
    assert not same_requisition(
        _listing(body="Join us."), _listing(body="Join us.", **CAREERS)
    ).same


def test_a_reworded_posting_is_not_merged_on_title_alone() -> None:
    other = (
        BODY.replace("payment infrastructure", "our marketing site").replace(
            "PostgreSQL", "WordPress"
        )
        + " Plus a completely separate paragraph about a different team and product line."
    )

    assert not same_requisition(_listing(), _listing(body=other, **CAREERS)).same


def test_requisition_ids_are_normalized() -> None:
    assert requisition_id("Job ID: jr_004521") == "JR-004521"
    assert requisition_id("No identifier here") is None


# --------------------------------------------------------------------------
# Versions
# --------------------------------------------------------------------------


def _state(text: str, **overrides: object) -> dict:
    reading = extract(text)
    pay = reading.compensation
    state = {
        "content_hash": str(hash(text)),
        "title": "Backend Engineer",
        "location": "Remote",
        "salary_min": pay.minimum if pay else None,
        "salary_max": pay.maximum if pay else None,
        "salary_currency": pay.currency if pay else None,
        "salary_period": pay.period if pay else None,
        "requirements_json": reading.as_json(),
    }
    state.update(overrides)
    return state


OLD = "Requirements\n- Python\nThe annual salary range is $150,000 - $190,000 USD."
NEW = (
    "Requirements\n- Python\n- Kubernetes\n- Master's degree\n"
    "The annual salary range is $140,000 - $170,000 USD."
)


def test_a_pay_cut_and_new_requirements_are_named() -> None:
    changes = {change.field: change for change in diff(_state(OLD), _state(NEW))}

    assert changes["pay"].before == "150,000–190,000 USD per year"
    assert changes["pay"].after == "140,000–170,000 USD per year"
    assert changes["required_skills"].after == ["Kubernetes"]
    assert changes["education"].after == "required master"


def test_an_edit_that_touches_nothing_comparable_is_still_a_text_change() -> None:
    changes = diff(_state(OLD), _state(OLD, content_hash="edited"))

    assert [change.field for change in changes] == ["text"]


def test_a_reading_from_before_extraction_is_not_all_skills_removed() -> None:
    before = _state(OLD, requirements_json=None)

    assert "required_skills" not in {change.field for change in diff(before, _state(NEW))}


def test_unstated_pay_is_described_as_not_stated() -> None:
    assert describe_pay(snapshot({"salary_min": None, "salary_max": None})) == "not stated"
