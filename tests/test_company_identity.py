"""Company identity needs evidence beyond a board slug; all names invented."""

import uuid
from datetime import UTC, datetime

import pytest

from apps.worker import discover_company_job as job
from packages.core.models import Company, CompanyCrawlState
from packages.crawler import duplicate_boards as boards
from packages.crawler.find_boards import Resolved


def company(name: str, website: str | None = None, **kwargs) -> Company:
    return Company(id=uuid.uuid4(), name=name, supplied_website=website, **kwargs)


def test_a_similar_name_and_board_are_not_identity() -> None:
    a = company("Juniper Labs", "https://juniperlabs.example")
    b = company("Juniper", "https://juniper.example")
    assert boards.identity_reason(a, b, ats="ashby", slug="juniper") is None


def test_the_same_website_is_identity() -> None:
    a = company("Legacy Finch", "https://www.finch.example/careers")
    b = company("Finch", "https://finch.example")
    assert boards.identity_reason(a, b, ats="ashby", slug="finch")


@pytest.mark.parametrize(
    "confirmation", ["website agrees with the name", "slug is the website's .com name", None]
)
def test_name_or_slug_confirmation_is_not_domain_evidence(confirmation) -> None:
    a = company("Juniper Labs", "https://juniperlabs.example")
    b = company("Juniper", "https://juniper.example")
    evidence = {
        "ats": "ashby",
        "slug": "juniper",
        "lead": a.supplied_website,
        "confirmed_by": confirmation,
    }
    assert boards.identity_reason(a, b, ats="ashby", slug="juniper", evidence=evidence) is None


def test_name_overlap_plus_board_website_evidence() -> None:
    a = company("Finch.io", "https://finch.example")
    b = company("Finch", None)
    evidence = {
        "ats": "ashby",
        "slug": "finch",
        "lead": a.supplied_website,
        "confirmed_by": "board names the website",
    }
    assert boards.identity_reason(a, b, ats="ashby", slug="finch", evidence=evidence)
    assert boards.identity_reason(a, b, ats="ashby", slug="other", evidence=evidence) is None


@pytest.mark.parametrize("kind", ["rename", "acquisition"])
def test_recorded_relationship_names_both_companies(kind: str) -> None:
    a = company(
        "Legacy Finch",
        "https://oldfinch.example",
        source_evidence={
            "relationship": {
                "kind": kind,
                "from": "Legacy Finch",
                "to": "Finch",
                "source_url": "https://finch.example/news",
            }
        },
    )
    b = company("Finch", "https://finch.example")
    assert boards.identity_reason(a, b, ats="ashby", slug="finch")
    a.name = "Different Finch"
    assert boards.identity_reason(a, b, ats="ashby", slug="finch") is None


def test_shared_ats_domain_is_not_a_company_website() -> None:
    a = company("Juniper Labs", "https://jobs.ashbyhq.com/juniper-labs")
    b = company("Juniper", "https://jobs.ashbyhq.com/juniper")
    assert boards.identity_reason(a, b, ats="ashby", slug="juniper") is None


async def test_verify_collision_does_not_set_aside_or_suppress_retry(db_session) -> None:
    holder = company(
        "Juniper",
        "https://juniper.example",
        ats_type="ashby",
        slug="juniper",
        source_status="verified",
    )
    target = company("Juniper Labs", "https://juniperlabs.example", source_status="hint")
    db_session.add_all([holder, target])
    await db_session.flush()
    state = CompanyCrawlState(company_id=target.id, discovery_attempts=1)
    db_session.add(state)
    await db_session.flush()
    now = datetime.now(UTC)
    found = Resolved(target.name, "ashby", "juniper", "https://jobs.ashbyhq.com/juniper", 2)
    assert not await job.verify_board(
        db_session,
        target,
        state,
        found,
        evidence={"method": "discovery_from_name"},
        via="name",
        now=now,
    )
    assert target.source_evidence["category"] == "name_collision"
    assert target.source_evidence["unconfirmed"] == ["ashby/juniper"]
    assert "duplicate_of" not in target.source_evidence
    assert now < state.discovery_next_at < now + boards.RECHECK_AFTER


@pytest.mark.parametrize("vendor", ["ashby", "workday"])
async def test_a_rejected_site_board_is_not_reported_empty_or_unreachable(
    monkeypatch, vendor
) -> None:
    from packages.crawler import find_boards

    async def page(url, fetcher):
        return find_boards.SiteReading(board=(vendor, "finch"), read=True)

    async def probe(fetcher, vendor, slug):
        return 2, "https://jobs.example/finch", None

    async def reject(found):
        return False

    monkeypatch.setattr(find_boards, "from_url", page)
    monkeypatch.setattr(find_boards, "_probe", probe)
    result, reading = await find_boards.resolve_site(
        "Finch", object(), url="https://finch.example", guess=False, accept=reject
    )
    assert not isinstance(result, Resolved)
    assert reading.board is None
    assert reading.unreachable is None


@pytest.mark.parametrize("depth", [0, 1, 3])
@pytest.mark.parametrize(
    ("domain", "lead"),
    [
        ("oldfinch.example", "https://oldfinch.example/careers"),
        ("oldfinch.example", "https://www.oldfinch.example/jobs"),
        ("www.oldfinch.example", "https://oldfinch.example"),
        ("OLDFINCH.EXAMPLE", "http://WWW.OLDFINCH.EXAMPLE"),
    ],
)
def test_own_website_link_proves_identity_through_history(depth, domain, lead) -> None:
    # No name overlap; the stored company domain, not a supplied URL, is the
    # authority for this rule. A rename need not preserve any part of a name.
    target = company("Old Finch", "https://marketing.example", domain=domain)
    holder = company("Canopy", "https://canopy.example")
    evidence = {"method": "discovery_from_url", "lead": lead, "ats": "ashby", "slug": "canopy"}
    for _ in range(depth):
        evidence = {"method": "duplicate_board", "previous": evidence}
    target.source_evidence = evidence
    reason = boards.identity_reason(target, holder, ats="ashby", slug="canopy")
    assert reason and "own website" in reason


@pytest.mark.parametrize(
    "changed",
    [
        {"lead": "https://another.example/careers"},
        {"lead": "https://careers.oldfinch.example"},
        {"lead": "https://oldfinch.example.attacker.example"},
        {"lead": "https://oldfinch.example@another.example"},
        {"lead": "https://another.example/oldfinch.example"},
        {"lead": None},
        {"lead": "not a URL"},
        {"method": "discovery_from_directory"},
        {"method": "discovery_from_name"},
        {"ats": "greenhouse"},
        {"slug": "other-board"},
    ],
)
def test_own_website_proof_requires_the_method_host_and_board(changed) -> None:
    target = company("Old Finch", "https://oldfinch.example", domain="oldfinch.example")
    holder = company("Canopy", "https://canopy.example")
    evidence = {
        "method": "discovery_from_url",
        "lead": "https://oldfinch.example/jobs",
        "ats": "ashby",
        "slug": "canopy",
        **changed,
    }
    target.source_evidence = {"previous": evidence}
    assert boards.identity_reason(target, holder, ats="ashby", slug="canopy") is None


def test_a_supplied_website_does_not_substitute_for_the_company_domain() -> None:
    target = company("Old Finch", "https://oldfinch.example")
    holder = company("Canopy", "https://canopy.example")
    evidence = {
        "method": "discovery_from_url",
        "lead": target.supplied_website,
        "ats": "ashby",
        "slug": "canopy",
    }
    assert (
        boards.identity_reason(target, holder, ats="ashby", slug="canopy", evidence=evidence)
        is None
    )


async def test_verification_accepts_current_own_website_proof(db_session) -> None:
    holder = company(
        "Canopy",
        "https://canopy.example",
        ats_type="ashby",
        slug="canopy",
        source_status="verified",
    )
    target = company(
        "Old Finch", "https://oldfinch.example", domain="oldfinch.example", source_status="hint"
    )
    db_session.add_all([holder, target])
    await db_session.flush()
    state = CompanyCrawlState(company_id=target.id, discovery_attempts=1)
    db_session.add(state)
    await db_session.flush()
    evidence = {
        "method": "discovery_from_url",
        "lead": "https://www.oldfinch.example/jobs",
        "ats": "ashby",
        "slug": "canopy",
    }
    found = Resolved(target.name, "ashby", "canopy", "https://jobs.ashbyhq.com/canopy", 2)
    now = datetime.now(UTC)
    assert not await job.verify_board(
        db_session, target, state, found, evidence=evidence, via="url", now=now
    )
    assert target.source_evidence["method"] == "duplicate_board"
    assert target.source_evidence["duplicate_of"] == str(holder.id)
    assert "own website" in target.source_evidence["identity_reason"]
    assert state.discovery_next_at == now + boards.RECHECK_AFTER
