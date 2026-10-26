"""Technical or negated mentions of remote are not a working-mode offer."""

import pytest

from packages.matching.locality import reads_as_remote


@pytest.mark.parametrize(
    "technical",
    [
        "remote sensing",
        "remote procedure calls",
        "remote access tooling",
        "remote desktop tooling",
        "remote monitoring tools",
    ],
)
@pytest.mark.parametrize("field", ["title", "description"])
def test_technical_remote_mentions_do_not_establish_work_mode(technical, field):
    fields = {"title": "Engineer", "location": "Austin, TX", "description": "Build tools."}
    fields[field] = f"Build {technical}"
    assert not reads_as_remote(**fields)


@pytest.mark.parametrize(
    "description",
    [
        "This role is not remote.",
        "This is not a remote position.",
        "We do not offer remote work.",
        "No remote work is available.",
        "Remote work is not available.",
        "You cannot work remotely.",
        "Remote work is unavailable.",
        "Remote work is prohibited.",
    ],
)
def test_negated_remote_prose_is_not_an_offer(description):
    assert not reads_as_remote(title="Engineer", location="Austin, TX", description=description)


@pytest.mark.parametrize(
    "description",
    [
        "This role is remote.",
        "This is a remote position.",
        "You can work remotely.",
        "This position offers work from home.",
        "WFH is available.",
        "This is a home-based role.",
        "We are a fully distributed team.",
        "You can work from anywhere.",
    ],
)
def test_positive_work_mode_evidence_is_preserved(description):
    assert reads_as_remote(title="Engineer", location="Austin, TX", description=description)


@pytest.mark.parametrize("title", ["Engineer (Hybrid)", "On-site Engineer"])
def test_explicit_metadata_overrides_positive_remote_prose(title):
    assert not reads_as_remote(
        title=title, location="Austin, TX", description="This role is remote."
    )


@pytest.mark.parametrize(
    ("title", "location"),
    [("Engineer (Remote)", "Austin, TX"), ("Engineer", "Remote, US")],
)
def test_clear_remote_metadata_still_establishes_work_mode(title, location):
    assert reads_as_remote(title=title, location=location)


@pytest.mark.parametrize("title", ["Engineer (not remote)", "Non-remote Engineer"])
def test_denied_metadata_does_not_establish_remote_work(title):
    assert not reads_as_remote(title=title, location="Austin, TX")


@pytest.mark.parametrize("description", ["No WFH is offered.", "We do not offer work from home."])
def test_existing_work_mode_aliases_respect_a_denial(description):
    assert not reads_as_remote(title="Engineer", location="Austin, TX", description=description)


def test_a_separate_affirmative_offer_survives_an_unrelated_negated_mention():
    assert reads_as_remote(
        title="Engineer",
        location="Austin, TX",
        description="The internship is not remote. This full-time position is remote.",
    )


def test_denied_metadata_overrides_positive_company_prose():
    assert not reads_as_remote(
        title="Engineer (not remote)",
        location="Austin, TX",
        description="We are a remote-first company.",
    )
