"""The two doors into `companies`, and what each of them must not destroy.

`packages/crawler/registry.py` is the only path from a spreadsheet or the
curated YAML into the table the dispatcher reads. Both writers are idempotent
and both have one thing they must refuse to do, and those refusals are the
whole reason the module exists rather than an `INSERT` at each call site:

- re-importing a sheet must not undo what discovery learned;
- syncing the YAML must not demote a board discovery verified more recently,
  and must not resurrect a retired one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select

from packages.core.enums import SourceStatus
from packages.core.models import Company, CompanyCrawlState
from packages.crawler.company_csv import Classified, Row, TriageReport
from packages.crawler.extract import CompanySeed, RetiredSeed
from packages.crawler.registry import (
    RETIRED_METHOD,
    register_candidates,
    status_for,
    sync_registry,
)
from packages.crawler.registry_health import diagnose_registry


def _row(name: str, *, website: str = "", career: str = "", url: str = "", row: int = 1) -> Row:
    """A row as `read_rows` builds one.

    `url` is the lead: the careers URL when it is a usable one, the website
    otherwise. A `career` that is a search link leaves `url` empty, which is
    why the two are separate arguments here rather than one.
    """
    lead = url or career or website
    return Row(url=lead, name=name, website=website, career_hint=career, source_row=row)


def _board(name: str, *, ats: str = "lever", slug: str = "acme") -> Classified:
    return Classified(
        row=_row(name, url=f"https://jobs.lever.co/{slug}"),
        ats=ats,
        slug=slug,
        reason="a board we can already crawl",
    )


def _report(
    *,
    promotable: list[Classified] | None = None,
    candidates: list[Classified] | None = None,
    unusable: list[Classified] | None = None,
) -> TriageReport:
    return TriageReport(
        promotable=promotable or [],
        bespoke=[],
        candidates=candidates or [],
        unusable=unusable or [],
    )


# --------------------------------------------------------------------------
# What a row is worth before anything has been fetched
# --------------------------------------------------------------------------


def test_a_board_shaped_url_is_unverified_not_verified() -> None:
    """The right shape is not evidence. `jobs.lever.co/xchg` can still 404."""
    assert status_for(_board("Acme")) is SourceStatus.UNVERIFIED


def test_a_website_or_a_careers_page_is_a_hint() -> None:
    website = Classified(row=_row("Acme", website="https://acme.example"), reason="website only")
    career = Classified(
        row=_row("Beta", career="https://beta.example/careers"), reason="careers page"
    )
    # `read_rows` leaves `url` empty when the careers column holds a search
    # link and there is no website, so this is that row exactly.
    searched = Classified(
        row=Row(url="", name="Gamma", career_hint="https://www.google.com/search?q=gamma"),
        reason="a search link and no website — no lead",
    )
    assert status_for(website) is SourceStatus.HINT
    assert status_for(career) is SourceStatus.HINT
    # A search link is not a lead: `read_rows` leaves `url` empty for one, and
    # a company registered as a hint on the strength of it would be handed to
    # discovery with nothing to fetch.
    assert status_for(searched) is SourceStatus.NO_WEBSITE


def test_a_row_with_no_lead_is_a_different_problem_from_a_failure() -> None:
    """`no_website` needs a human; `failed` means discovery tried."""
    assert status_for(Classified(row=_row("Nothing"), reason="no lead")) is SourceStatus.NO_WEBSITE


# --------------------------------------------------------------------------
# register_candidates
# --------------------------------------------------------------------------


async def test_every_bucket_lands_in_the_table(db_session) -> None:
    report = _report(
        promotable=[_board("Acme")],
        candidates=[Classified(row=_row("Beta", website="https://beta.example"), reason="website")],
        unusable=[Classified(row=_row("Nothing"), reason="no lead")],
    )

    outcome = await register_candidates(db_session, report, source_file="sheet.csv")

    assert (outcome.created, outcome.updated, outcome.protected) == (3, 0, 0)
    rows = {c.name: c.source_status for c in (await db_session.scalars(select(Company))).all()}
    assert rows == {
        "Acme": SourceStatus.UNVERIFIED.value,
        "Beta": SourceStatus.HINT.value,
        "Nothing": SourceStatus.NO_WEBSITE.value,
    }


async def test_provenance_survives_the_import(db_session) -> None:
    """Which file and which line, so a name collision is visible not silent."""
    entry = Classified(
        row=_row(
            "Beta", website="https://beta.example", career="https://g.example/?q=beta", row=42
        ),
        reason="website",
    )
    await register_candidates(db_session, _report(candidates=[entry]), source_file="sheet.csv")

    beta = await db_session.scalar(select(Company).where(Company.name == "Beta"))
    assert beta.source_file == "sheet.csv"
    assert beta.source_row == 42
    assert beta.supplied_website == "https://beta.example"
    assert beta.supplied_career_url == "https://g.example/?q=beta"
    assert beta.domain == "beta.example"
    assert beta.source_evidence["method"] == "csv_import"


async def test_reimporting_creates_nothing_and_changes_no_status(db_session) -> None:
    report = _report(
        promotable=[_board("Acme")],
        candidates=[Classified(row=_row("Beta", website="https://beta.example"), reason="website")],
    )
    await register_candidates(db_session, report, source_file="sheet.csv")

    second = await register_candidates(db_session, report, source_file="sheet.csv")

    assert second.created == 0, "idempotent on the company name"
    assert await db_session.scalar(select(func.count()).select_from(Company)) == 2


async def test_a_verified_row_is_never_downgraded_by_a_reimport(db_session) -> None:
    """The protection that makes re-import safe after discovery has run.

    Without it, re-importing the sheet a discovery run was started from puts
    every verified board back to `hint` — and the next sweep re-discovers
    boards it already knows, at 60s a host.
    """
    entry = Classified(row=_row("Beta", website="https://beta.example"), reason="website")
    await register_candidates(db_session, _report(candidates=[entry]), source_file="sheet.csv")

    verified_at = datetime.now(UTC) - timedelta(hours=1)
    beta = await db_session.scalar(select(Company).where(Company.name == "Beta"))
    beta.source_status = SourceStatus.VERIFIED.value
    beta.ats_type = "greenhouse"
    beta.slug = "betaco"
    beta.source_verified_at = verified_at
    beta.source_evidence = {"method": "discovery_from_url"}
    await db_session.flush()

    outcome = await register_candidates(
        db_session, _report(candidates=[entry]), source_file="sheet.csv"
    )

    assert outcome.protected == 1
    beta = await db_session.scalar(select(Company).where(Company.name == "Beta"))
    assert beta.source_status == SourceStatus.VERIFIED.value
    assert (beta.ats_type, beta.slug) == ("greenhouse", "betaco")
    assert beta.source_verified_at == verified_at
    assert beta.source_evidence["method"] == "discovery_from_url"


async def test_an_unresolved_row_is_owed_discovery_and_a_board_is_not(db_session) -> None:
    """NULL `discovery_next_at` means as soon as possible, as `next_due_at` does."""
    report = _report(
        promotable=[_board("Acme")],
        candidates=[Classified(row=_row("Beta", website="https://beta.example"), reason="website")],
    )
    await register_candidates(db_session, report, source_file="sheet.csv")

    states = {
        company.name: state
        for company, state in (
            await db_session.execute(
                select(Company, CompanyCrawlState).join(
                    CompanyCrawlState, CompanyCrawlState.company_id == Company.id
                )
            )
        ).all()
    }
    # Every imported row that is not a verified board is owed discovery — the
    # board-shaped one included, because its shape has not been checked.
    assert set(states) == {"Acme", "Beta"}
    assert all(state.discovery_next_at is None for state in states.values())


async def test_a_nameless_row_is_skipped_rather_than_stored_as_blank(db_session) -> None:
    """The name is the idempotency key, so a blank one would collapse rows."""
    await register_candidates(
        db_session,
        _report(candidates=[Classified(row=_row("  ", website="https://x.example"), reason="x")]),
        source_file="sheet.csv",
    )
    assert await db_session.scalar(select(func.count()).select_from(Company)) == 0


# --------------------------------------------------------------------------
# sync_registry
# --------------------------------------------------------------------------


async def test_a_seed_becomes_a_verified_company(db_session) -> None:
    seeds = [CompanySeed(name="Acme", slug="acmeco", ats="greenhouse", checked="2026-09-07")]

    outcome = await sync_registry(db_session, seeds)

    assert (outcome.created, outcome.verified) == (1, 1)
    acme = await db_session.scalar(select(Company).where(Company.name == "Acme"))
    assert acme.source_status == SourceStatus.VERIFIED.value
    assert (acme.ats_type, acme.slug) == ("greenhouse", "acmeco")
    assert acme.source_verified_at == datetime(2026, 9, 7, tzinfo=UTC)
    assert acme.source_evidence["method"] == "registry_sync"


async def test_syncing_twice_changes_nothing(db_session) -> None:
    seeds = [CompanySeed(name="Acme", slug="acmeco", checked="2026-09-07")]
    await sync_registry(db_session, seeds)

    second = await sync_registry(db_session, seeds)

    assert second.created == 0
    assert await db_session.scalar(select(func.count()).select_from(Company)) == 1


async def test_sync_promotes_an_imported_candidate_to_its_accepted_board(db_session) -> None:
    """The two doors meet on one row: the sheet registered it, the YAML accepts it."""
    entry = Classified(row=_row("Acme", website="https://acme.example"), reason="website")
    await register_candidates(db_session, _report(candidates=[entry]), source_file="sheet.csv")

    outcome = await sync_registry(
        db_session, [CompanySeed(name="Acme", slug="acmeco", checked="2026-09-07")]
    )

    assert (outcome.created, outcome.verified) == (0, 1)
    acme = await db_session.scalar(select(Company).where(Company.name == "Acme"))
    assert acme.source_status == SourceStatus.VERIFIED.value
    assert acme.slug == "acmeco"
    assert acme.supplied_website == "https://acme.example", "the sheet's lead is not discarded"


async def test_newer_evidence_in_the_database_wins_over_an_older_seed(db_session) -> None:
    """A board discovery verified this morning is not demoted by last week's file."""
    await sync_registry(db_session, [CompanySeed(name="Acme", slug="old", checked="2026-09-01")])
    acme = await db_session.scalar(select(Company).where(Company.name == "Acme"))
    acme.slug = "discovered"
    acme.source_verified_at = datetime(2026, 9, 10, tzinfo=UTC)
    acme.source_evidence = {"method": "discovery_from_url"}
    await db_session.flush()

    outcome = await sync_registry(
        db_session, [CompanySeed(name="Acme", slug="old", checked="2026-09-01")]
    )

    assert outcome.newer_in_db == 1
    acme = await db_session.scalar(select(Company).where(Company.name == "Acme"))
    assert acme.slug == "discovered", "the seed must not overwrite newer evidence"
    assert acme.source_evidence["method"] == "discovery_from_url"


async def test_a_seed_checked_later_than_the_stored_evidence_does_apply(db_session) -> None:
    """The other direction of the same comparison, so it is a date test not a no-op."""
    await sync_registry(db_session, [CompanySeed(name="Acme", slug="old", checked="2026-09-01")])
    acme = await db_session.scalar(select(Company).where(Company.name == "Acme"))
    acme.source_verified_at = datetime(2026, 9, 1, tzinfo=UTC)
    await db_session.flush()

    await sync_registry(
        db_session, [CompanySeed(name="Acme", slug="renamed", checked="2026-09-12")]
    )

    acme = await db_session.scalar(select(Company).where(Company.name == "Acme"))
    assert acme.slug == "renamed"


async def test_a_retired_board_cannot_be_resurrected(db_session) -> None:
    """Not by a guard here — by `load_seed` never yielding it.

    This test asserts the reachability rather than a branch, because that is
    where the guarantee actually lives: `sync_registry` marks verified whatever
    it is handed, so if a retired entry ever reached it the board would come
    back. The protection is that the reader of the file does not return them.
    """
    from packages.crawler.extract import load_seed

    path = "seeds/companies.yaml"
    names = {seed.slug for seed in load_seed(path)}
    assert names, "the curated registry still has live entries"

    import yaml

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    retired = {entry["slug"] for entry in (raw.get("retired") or [])}
    assert retired, "and a retired section holding the evidence"
    assert not (names & retired), "a retired slug is never handed to the sync"

    seed = load_seed(path)[0]
    await sync_registry(db_session, [seed])
    stored = await db_session.scalar(select(Company).where(Company.name == seed.name))
    assert stored is not None and stored.slug == seed.slug
    assert stored.slug not in retired


# --------------------------------------------------------------------------
# Retired boards, evidence history, and the diagnosis
# --------------------------------------------------------------------------
#
# The live defect: 119 rows created by the inline crawl before `slug` existed,
# all backfilled to `no_website`. 107 matched a live registry entry and 14 a
# retired one, and the dashboard asked for a URL for every one of them.


def _legacy_row(name: str, *, ats: str = "greenhouse") -> Company:
    """A row exactly as the inline crawl left it and the migration backfilled it."""
    return Company(
        name=name,
        ats_type=ats,
        slug=None,
        last_polled_at=datetime(2026, 8, 28, tzinfo=UTC),
        source_status=SourceStatus.NO_WEBSITE.value,
        source_evidence={"method": "backfill_no_url"},
    )


def _retired(name: str | None, slug: str = "gone", ats: str = "greenhouse") -> RetiredSeed:
    return RetiredSeed(
        name=name, slug=slug, ats=ats, checked="2026-09-07", state="missing", api_status=404
    )


async def test_a_legacy_row_is_repaired_and_keeps_what_it_said_before(db_session) -> None:
    db_session.add(_legacy_row("Acme"))
    await db_session.flush()

    outcome = await sync_registry(
        db_session, [CompanySeed(name="Acme", slug="acmeco", checked="2026-09-07")]
    )

    assert outcome.verified == 1
    acme = await db_session.scalar(select(Company).where(Company.name == "Acme"))
    assert acme.source_status == SourceStatus.VERIFIED.value
    assert acme.slug == "acmeco"
    assert acme.source_evidence["previous"] == {
        "source_status": "no_website",
        "source_evidence": {"method": "backfill_no_url"},
    }


async def test_a_retired_board_row_is_marked_not_left_asking_for_a_url(db_session) -> None:
    db_session.add(_legacy_row("Gone Inc"))
    await db_session.flush()

    outcome = await sync_registry(db_session, [], retired=[_retired("Gone Inc", slug="goneinc")])

    assert outcome.retired == 1
    gone = await db_session.scalar(select(Company).where(Company.name == "Gone Inc"))
    assert gone.source_status == SourceStatus.FAILED.value
    assert gone.slug is None, "the condemned board cannot be named to the dispatcher"
    assert gone.source_evidence["method"] == RETIRED_METHOD
    assert gone.source_evidence["slug"] == "goneinc"
    assert gone.source_evidence["api_status"] == 404
    assert gone.source_evidence["previous"]["source_status"] == "no_website"


async def test_marking_retired_boards_is_idempotent(db_session) -> None:
    db_session.add(_legacy_row("Gone Inc"))
    await db_session.flush()
    retired = [_retired("Gone Inc")]

    await sync_registry(db_session, [], retired=retired)
    second = await sync_registry(db_session, [], retired=retired)

    assert second.retired == 0
    gone = await db_session.scalar(select(Company).where(Company.name == "Gone Inc"))
    assert "previous" not in gone.source_evidence["previous"].get("source_evidence", {}), (
        "a second run must not nest the history inside itself"
    )


async def test_a_retirement_never_demotes_newer_verification(db_session) -> None:
    """Discovery found a board after the sweep condemned the old one."""
    db_session.add(
        Company(
            name="Moved Inc",
            ats_type="ashby",
            slug="moved",
            source_status=SourceStatus.VERIFIED.value,
            source_verified_at=datetime(2026, 9, 10, tzinfo=UTC),
            source_evidence={"method": "discovery_from_url"},
        )
    )
    await db_session.flush()

    outcome = await sync_registry(db_session, [], retired=[_retired("Moved Inc")])

    assert (outcome.retired, outcome.retired_newer_in_db) == (0, 1)
    moved = await db_session.scalar(select(Company).where(Company.name == "Moved Inc"))
    assert moved.source_status == SourceStatus.VERIFIED.value
    assert moved.slug == "moved"


async def test_a_name_retired_on_one_board_and_live_on_another_stays_live(db_session) -> None:
    """Temporal and Runway: retired on Greenhouse, re-added on Ashby."""
    db_session.add(_legacy_row("Temporal", ats="greenhouse"))
    await db_session.flush()

    outcome = await sync_registry(
        db_session,
        [CompanySeed(name="Temporal", slug="temporal", ats="ashby", checked="2026-09-12")],
        retired=[_retired("Temporal", slug="temporaltechnologies")],
    )

    assert outcome.moved_boards == ["Temporal"]
    assert outcome.retired == 0
    row = await db_session.scalar(select(Company).where(Company.name == "Temporal"))
    assert row.source_status == SourceStatus.VERIFIED.value
    assert (row.ats_type, row.slug) == ("ashby", "temporal")
    assert row.source_evidence["retired_board"]["slug"] == "temporaltechnologies"


async def test_a_retired_entry_with_no_row_creates_nothing(db_session) -> None:
    await sync_registry(db_session, [], retired=[_retired("Never Crawled")])

    assert await db_session.scalar(select(func.count()).select_from(Company)) == 0


async def test_the_diagnosis_names_every_disagreement_and_changes_nothing(db_session) -> None:
    db_session.add_all([_legacy_row("Acme"), _legacy_row("Gone Inc")])
    await db_session.flush()
    seeds = [
        CompanySeed(name="Acme", slug="acmeco", checked="2026-09-07"),
        CompanySeed(name="Beta", slug="betaco", checked="2026-09-07"),
    ]
    retired = [_retired("Gone Inc"), _retired(None, slug="nameless")]

    health = await diagnose_registry(db_session, seeds, retired)

    codes = {problem.code: problem for problem in health.problems}
    assert codes["registry_rows_missing"].examples == ("Beta",)
    assert codes["registry_rows_stale"].examples == ("Acme",)
    assert codes["retired_board_unmarked"].examples == ("Gone Inc",)
    assert codes["retired_entry_nameless"].examples == ("nameless",)
    assert "make registry-sync" in codes["registry_rows_stale"].fix
    assert (health.rows, health.fetchable) == (2, 0)
    acme = await db_session.scalar(select(Company).where(Company.name == "Acme"))
    assert acme.source_status == SourceStatus.NO_WEBSITE.value, "diagnosis is read-only"


async def test_after_a_sync_the_diagnosis_is_clean(db_session) -> None:
    db_session.add_all([_legacy_row("Acme"), _legacy_row("Gone Inc")])
    await db_session.flush()
    seeds = [CompanySeed(name="Acme", slug="acmeco", checked="2026-09-07")]
    retired = [_retired("Gone Inc")]

    await sync_registry(db_session, seeds, retired=retired)
    health = await diagnose_registry(db_session, seeds, retired)

    assert health.ok, health.summary()
    assert health.fetchable == 1


def test_the_retired_section_is_read_by_its_own_loader(tmp_path) -> None:
    from packages.crawler.extract import load_retired, load_seed

    path = tmp_path / "companies.yaml"
    path.write_text(
        "companies:\n- name: Live\n  slug: live\nretired:\n"
        "- name: Dead\n  slug: dead\n  api_status: 404\n"
    )

    assert [seed.name for seed in load_seed(str(path))] == ["Live"]
    assert [(r.name, r.api_status) for r in load_retired(str(path))] == [("Dead", 404)]


def test_a_malformed_retired_section_raises_rather_than_reading_empty(tmp_path) -> None:
    import pytest

    from packages.crawler.extract import SeedFileError, load_retired

    path = tmp_path / "companies.yaml"
    path.write_text("companies: []\nretired: {name: Dead}\n")

    with pytest.raises(SeedFileError):
        load_retired(str(path))


async def test_the_dry_run_computes_the_real_change_and_writes_nothing(
    committing_sessionmaker, monkeypatch, tmp_path
) -> None:
    """Same code path as the write, rolled back — so the preview cannot lie."""
    from packages.core import db as core_db
    from scripts import registry_sync

    monkeypatch.setattr(core_db, "get_sessionmaker", lambda: committing_sessionmaker)
    path = tmp_path / "companies.yaml"
    path.write_text("companies:\n- name: DryRun Co\n  slug: dryrun\n  checked: '2026-09-07'\n")

    message = await registry_sync.run(str(path), dry_run=True)

    assert message.startswith("DRY RUN")
    assert "1 created" in message
    async with committing_sessionmaker() as session:
        count = await session.scalar(
            select(func.count()).select_from(Company).where(Company.name == "DryRun Co")
        )
    assert count == 0
