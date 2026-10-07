"""The Data API Builder beside the API: what it may serve, and to whom.

`make dab` runs Microsoft's Data API Builder over three tables, so they can
be queried with `$filter`, `$select`, `$orderby` and paging, or by GraphQL,
without a route being written for each question. The owner asked for it on
2026-10-06, after the DP-800 course's section on it.

It is a second door onto the database, and the first one has guards this one
does not inherit: `apps/api/middleware.py` refuses callers that are not on
this machine, and every route was written knowing what it returns. This serves
whatever its config names. So the config and the launcher are held here:

- three sources and no others. Résumés, profiles, applications and recruiter
  mail are behind the API and stay there (CLAUDE.md §2.8);
- read and nothing else, twice: by the config's permissions, and by a
  connection whose transactions Postgres itself refuses to write in;
- this machine only. CLAUDE.md §3 records the dashboard being bound to the
  network by a default nobody had read;
- no MCP endpoint. The course's own rule is that one is never left anonymous,
  and `apps/mcp` already is this project's, with its guards.

None of these start the tool. CI does not have it installed, and what matters
is in two files that can be read.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from scripts import run_dab

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "apps" / "dab" / "dab-config.json").read_text())
MAKEFILE = (ROOT / "Makefile").read_text()
VIEW_MIGRATION = (
    ROOT / "migrations" / "versions" / "d4e6f8a0b1c3_api_postings_view.py"
).read_text()

#: Made up, and put together here rather than written out. A secret scanner
#: reads `user:password@host` and `Username=...;Password=...` in one string as
#: a leaked credential, and failed pull request 125 on a line of this file
#: that held nothing real.
USER, SECRET = "owner", "not-a-real-password"


def _url(secret: str = SECRET) -> str:
    return f"postgresql+asyncpg://{USER}:{secret}@localhost:5433/jobs"


#: Entity -> the database object it reads. Adding one is a decision about what
#: leaves the API's care, so it is made here as well as in the config.
SERVED = {
    "Company": "public.companies",
    "Posting": "public.api_postings",
    "Match": "public.matches",
}


def test_it_serves_three_sources_and_no_others() -> None:
    served = {name: entity["source"]["object"] for name, entity in CONFIG["entities"].items()}

    assert served == SERVED


def test_every_entity_is_read_only_in_the_config() -> None:
    for name, entity in CONFIG["entities"].items():
        actions = [
            action if isinstance(action, str) else action["action"]
            for permission in entity["permissions"]
            for action in permission["actions"]
        ]

        assert actions == ["read"], f"{name} allows {actions}"


def test_the_connection_refuses_writes_whatever_the_config_says() -> None:
    """Postgres is asked to refuse them, so a config mistake cannot write."""
    conn = run_dab.connection_string(_url())

    assert "default_transaction_read_only=on" in conn


def test_the_connection_string_is_built_from_the_projects_own_url() -> None:
    conn = run_dab.connection_string(_url())

    assert f"Host=localhost;Port=5433;Database=jobs;Username={USER};Password={SECRET};" in conn


def test_a_password_with_a_separator_in_it_survives() -> None:
    """`;` ends a value in this format and `@` ends the password in a URL."""
    conn = run_dab.connection_string(_url("a%3Bb%40c%22d"))
    quoted = '"a;b@c""d"'

    assert f"Password={quoted};" in conn


def test_the_password_is_never_written_into_the_config() -> None:
    assert CONFIG["data-source"]["connection-string"] == f"@env('{run_dab.CONN_ENV}')"


def test_it_listens_on_this_machine_only() -> None:
    env = run_dab.environment(_url(), {})

    assert env["ASPNETCORE_URLS"] == "http://127.0.0.1:5050"


def test_the_port_can_move_and_the_host_cannot() -> None:
    """Port 5000, the tool's default, is macOS's AirPlay receiver."""
    env = run_dab.environment(
        _url(), {"JOBRUNNER_DAB_PORT": "5099", "JOBRUNNER_DAB_HOST": "0.0.0.0"}
    )

    assert env["ASPNETCORE_URLS"] == "http://127.0.0.1:5099"


@pytest.mark.parametrize("port", ["", "abc", "0", "70000", "5050; rm"])
def test_a_port_that_is_not_one_is_refused(port: str) -> None:
    with pytest.raises(SystemExit):
        run_dab.environment(_url(), {"JOBRUNNER_DAB_PORT": port})


def test_it_is_started_through_the_launcher() -> None:
    """`dab start` typed into the Makefile would bind wherever the tool likes."""
    found = re.search(r"^dab:.*\n((?:\t.*\n)+)", MAKEFILE, re.M)
    assert found is not None, "no `dab` target in the Makefile"

    assert "scripts.run_dab" in found.group(1)
    assert "dab start" not in found.group(1)


def test_it_runs_from_its_own_folder() -> None:
    """The tool reads `.env` from where it starts, and the project's is not for it.

    Started in the repository root it also crashed: it could not parse that
    file. Its own folder has no `.env`, so it gets the one variable it is
    handed and nothing else.
    """
    assert run_dab.CONFIG_DIR == ROOT / "apps" / "dab"
    assert not (run_dab.CONFIG_DIR / ".env").exists()


def test_the_mcp_endpoint_is_off() -> None:
    assert CONFIG["runtime"]["mcp"]["enabled"] is False


def test_nothing_is_exported_as_telemetry() -> None:
    """The generated config exports traces to wherever `OTEL_*` points.

    Those carry each request's URL and query string. Nothing set them on this
    machine, but a variable left in a shell by another project would have
    started the export with nobody choosing it.
    """
    assert "telemetry" not in CONFIG["runtime"]


def test_the_view_is_declared_as_a_table_with_its_key() -> None:
    """Declared a view, 2.0.12 will not start: "Cannot define relationship for
    entity: Posting". It allows relationships between tables only, and the
    joins (a match's posting, a posting's company) are most of what GraphQL is
    for here. The course's own config does the same for its view.

    A view has no primary key for the tool to find, so the key is named.
    """
    posting = CONFIG["entities"]["Posting"]

    assert posting["source"]["type"] == "table"
    assert {"name": "id", "primary-key": True} in posting["fields"]
    assert set(posting["relationships"]) == {"company", "matches"}


def test_a_page_is_bounded() -> None:
    """The tool's own ceiling is 100,000 rows, and a posting is about 6 KB."""
    assert CONFIG["runtime"]["pagination"]["max-page-size"] <= 1000


def test_the_postings_view_leaves_the_vector_out() -> None:
    """Why there is a view at all: the tool cannot read a pgvector column.

    `postings` itself failed at start-up with "Reading as 'System.Object' is
    not supported for fields having DataTypeName 'public.vector'", so the view
    names its columns and that one is not among them.
    """
    created = re.search(
        r"CREATE VIEW api_postings AS\s+SELECT(.*?)FROM postings", VIEW_MIGRATION, re.S
    )
    assert created is not None, "the migration no longer creates api_postings from postings"
    columns = [column.strip() for column in created.group(1).split(",")]

    assert "id" in columns, "the tool needs the key"
    assert "*" not in columns, "named columns, so a later one is not served by default"
    assert not [column for column in columns if "embedding" in column]
