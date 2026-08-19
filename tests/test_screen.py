"""Reading the form before answering it.

§2.4 parks an unanswerable question *after* the fill, the screenshot, and the
review record. Two kinds of question are worth catching before any of that:
the ones whose honest answer ends the application, and the ones an employer
should not be asking.
"""

from __future__ import annotations

import uuid

from packages.ats.base import Question, QuestionKind
from packages.ats.screen import Finding, screen
from packages.core.models import Profile


def _profile(**kwargs: object) -> Profile:
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "candidate_id": uuid.uuid4(),
        "label": "default",
        "work_auth": "US citizen",
        "needs_sponsorship": False,
        "location": "Austin, TX",
    }
    defaults.update(kwargs)
    return Profile(**defaults)  # type: ignore[arg-type]


def _q(label: str, *, key: str = "f1", required: bool = False, help_text: str | None = None):
    return Question(
        key=key, label=label, kind=QuestionKind.TEXT, required=required, help_text=help_text
    )


# --------------------------------------------------------------------------
# Knock-outs
# --------------------------------------------------------------------------


def test_sponsorship_is_a_knock_out_only_when_the_profile_needs_it() -> None:
    question = [_q("Will you now or in the future require visa sponsorship?")]

    needs = screen(question, _profile(needs_sponsorship=True))
    does_not = screen(question, _profile(needs_sponsorship=False))

    assert needs.knock_outs and needs.knock_outs[0].finding is Finding.KNOCK_OUT
    assert not does_not.knock_outs


def test_a_clearance_request_is_a_knock_out() -> None:
    result = screen([_q("Do you hold an active TS/SCI security clearance?")], _profile())

    assert result.knock_outs


def test_work_authorization_with_no_profile_answer_is_a_knock_out() -> None:
    """§2.2 forbids generating one, so the field simply cannot be filled."""
    result = screen([_q("Are you legally authorized to work in the US?")], _profile(work_auth=""))

    assert result.knock_outs
    assert "§2.2" in result.knock_outs[0].reason


def test_work_authorization_with_a_profile_answer_is_not_flagged() -> None:
    result = screen(
        [_q("Are you legally authorized to work in the US?")],
        _profile(work_auth="US citizen"),
    )

    assert not result.knock_outs


def test_an_optional_relocation_question_is_not_a_knock_out() -> None:
    """Optional means it can be left alone. Only a required commitment
    actually gates the application."""
    optional = screen([_q("Are you willing to relocate?")], _profile())
    required = screen([_q("Are you willing to relocate?", required=True)], _profile())

    assert not optional.knock_outs
    assert required.knock_outs


# --------------------------------------------------------------------------
# Cautions
# --------------------------------------------------------------------------


def test_salary_history_is_flagged() -> None:
    result = screen([_q("What is your current salary?")], _profile())

    assert result.cautions
    assert result.cautions[0].finding is Finding.CAUTION
    assert "salary history" in result.cautions[0].reason


def test_date_of_birth_is_flagged() -> None:
    assert screen([_q("Date of birth")], _profile()).cautions


def test_citizenship_status_is_distinguished_from_work_authorization() -> None:
    """Asking *which* status, rather than whether the candidate may work, is
    the overreach — and the two look similar on a form."""
    result = screen([_q("Are you a US citizen or green card holder?")], _profile())

    assert result.cautions
    assert "specific immigration status" in result.cautions[0].reason


def test_a_voluntary_eeo_section_is_not_flagged() -> None:
    """These legitimately ask about protected characteristics and are
    optional by construction. Flagging them would fire on nearly every US
    application until nobody read the warnings."""
    result = screen(
        [
            _q(
                "Race / Ethnicity",
                help_text="Voluntary self-identification. You may decline to answer.",
            )
        ],
        _profile(),
    )

    assert not result.cautions


def test_help_text_is_read_as_well_as_the_label() -> None:
    result = screen(
        [_q("Compensation", help_text="Please list your most recent salary.")], _profile()
    )

    assert result.cautions


def test_the_question_label_is_preserved_verbatim() -> None:
    """§2.4: the owner sees the employer's exact wording, never a paraphrase."""
    label = "Do you now, or will you in the future, require sponsorship?"

    result = screen([_q(label)], _profile(needs_sponsorship=True))

    assert result.knock_outs[0].label == label


def test_an_ordinary_form_produces_nothing() -> None:
    """A screen that fires on everything is one nobody reads."""
    result = screen(
        [
            _q("First name"),
            _q("Email"),
            _q("LinkedIn profile"),
            _q("Why do you want to work here?"),
            _q("Résumé", required=True),
        ],
        _profile(),
    )

    assert not result.any_findings
    assert result.as_dict() == {"knock_outs": [], "cautions": []}


# --------------------------------------------------------------------------
# What it changes in the pipeline
# --------------------------------------------------------------------------


async def test_a_knock_out_parks_the_unattended_path(monkeypatch) -> None:
    """Auto-submitting into a question this profile visibly fails costs the
    owner nothing and a recruiter their time. That is the spray behaviour
    this project exists not to do."""
    from apps.worker import apply_job
    from packages.ats.screen import ScreenedQuestion, ScreenReport

    recorded: dict[str, object] = {}

    async def fake_transition(session, application, status, *, payload=None):
        recorded["status"] = status
        recorded["payload"] = payload

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("submitted despite a knock-out")

    monkeypatch.setattr(apply_job, "transition", fake_transition)
    monkeypatch.setattr(apply_job, "_submit", fail_if_called)

    knock_out = ScreenReport(
        knock_outs=[
            ScreenedQuestion("f1", "Do you require sponsorship?", Finding.KNOCK_OUT, "needs it")
        ]
    )

    await apply_job._decide(
        session=None,
        application=_stub_application(),
        profile=_profile(auto_submit=True, min_match_score=0.0),
        report=_complete_fill_report(),
        adapter=None,
        page=None,
        screening=knock_out,
    )

    assert recorded["status"].value == "needs_review"
    assert "Do you require sponsorship?" in (recorded["payload"] or {})["questions"]


async def test_an_owner_approval_outranks_a_knock_out(monkeypatch) -> None:
    """The owner looked at the form and said yes. §2.3's authorization is
    theirs to give, and a warning does not revoke it."""
    from apps.worker import apply_job
    from packages.ats.screen import ScreenedQuestion, ScreenReport

    submitted = {"called": False}

    async def fake_submit(*args, **kwargs):
        submitted["called"] = True

    monkeypatch.setattr(apply_job, "_submit", fake_submit)

    application = _stub_application()
    application.review_json = {"owner_approved": True}

    await apply_job._decide(
        session=None,
        application=application,
        profile=_profile(),
        report=_complete_fill_report(),
        adapter=None,
        page=None,
        screening=ScreenReport(
            knock_outs=[ScreenedQuestion("f1", "Clearance?", Finding.KNOCK_OUT, "asks")]
        ),
    )

    assert submitted["called"]


def _stub_application():
    from packages.core.models import Application

    return Application(
        id=uuid.uuid4(),
        candidate_id=uuid.uuid4(),
        profile_id=uuid.uuid4(),
        url="https://boards.greenhouse.io/acme/jobs/1",
        ats="greenhouse",
        status="running",
    )


def _complete_fill_report():
    from packages.ats.base import FillReport

    return FillReport(filled=[], skipped=[], unanswered=[])
