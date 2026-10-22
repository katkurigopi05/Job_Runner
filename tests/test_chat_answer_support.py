"""Answer support is a human judgment, separate from valid citation labels."""

import json

import pytest
from pydantic import ValidationError

from apps.api.answer_support import AnswerReview, freeze, score


def example(answer="Streamco requires Kafka [P1].\n\nThere are 12 matches."):
    return freeze(
        case_id="kafka-required",
        system="Use the evidence.",
        prompt="P1: Kafka: named under tooling, not required.\nMATCHES: 12",
        answer=answer,
        model="stub",
        labels=["P1"],
    )


def grade(record, index, verdict, quote=None, reviewer="owner"):
    data = record.model_dump()
    data["lines"][index].update(
        verdict=verdict,
        reviewer=reviewer,
        note="Reviewed the entire line.",
        quotes=[] if quote is None else [quote],
    )
    return AnswerReview.model_validate(data)


def test_freeze_includes_every_nonblank_line_and_pending_never_passes():
    record = example()
    assert [line.text for line in record.lines] == [
        "Streamco requires Kafka [P1].",
        "There are 12 matches.",
    ]
    with pytest.raises(ValueError, match="unfinished"):
        score([record])
    result = score([record], provisional=True)
    assert result.pending == 2
    assert result.support_rate is None
    assert result.complete is False


def test_real_failure_class_valid_citation_does_not_establish_support():
    record = grade(example(), 0, "contradicted", "Kafka: named under tooling, not required.")
    record = grade(record, 1, "supported", "MATCHES: 12")
    result = score([record])
    assert result.contradicted == 1
    assert result.supported == 1
    assert result.support_rate == 0.5
    assert result.owner_reviewed == 2


@pytest.mark.parametrize("field,value", [("answer", "different"), ("prompt", "different")])
def test_editing_frozen_material_invalidates_grades(field, value):
    data = example().model_dump()
    data[field] = value
    with pytest.raises(ValidationError):
        AnswerReview.model_validate(data)


def test_cannot_omit_a_line_or_use_a_quote_absent_from_context():
    data = example().model_dump()
    data["lines"].pop()
    with pytest.raises(ValidationError):
        AnswerReview.model_validate(data)
    with pytest.raises(ValidationError):
        grade(example(), 0, "supported", "Kafka is required")


def test_invented_citation_cannot_be_graded_supported():
    with pytest.raises(ValidationError):
        grade(example("Kafka [P99]."), 0, "supported", "Kafka")


def test_nonfactual_excluded_and_model_grades_reported_separately():
    record = grade(example("Hello.\n12 matches."), 0, "non_factual")
    record = grade(record, 1, "supported", "MATCHES: 12", reviewer="model")
    result = score([record])
    assert result.support_rate == 1
    assert result.owner_reviewed == 1
    assert result.model_reviewed == 1
    assert result.non_factual == 1


def test_empty_answer_is_not_perfect_and_duplicate_cases_rejected():
    result = score([example("")])
    assert result.support_rate is None
    assert result.empty_answers == 1
    with pytest.raises(ValueError, match="duplicate"):
        score([example(), example()], provisional=True)


@pytest.mark.parametrize(
    "evidence,answer",
    [
        ("Kafka is preferred, not required.", "Kafka is preferred [P1]."),
        ("Kafka, MQ, or a similar queue.", "Kafka is one alternative [P1]."),
        ("No applications at Exampleco.", "No applications at Exampleco."),
        ("12 postings mention Kafka; not all require it.", "12 postings mention Kafka."),
    ],
)
def test_supported_review_classes(evidence, answer):
    record = freeze(
        case_id="supported", system="s", prompt=evidence, answer=answer, model="stub", labels=["P1"]
    )
    assert score([grade(record, 0, "supported", evidence)]).support_rate == 1


def test_cli_prints_aggregates_and_does_not_leak_invalid_private_text(
    tmp_path, monkeypatch, capsys
):
    from types import SimpleNamespace

    from scripts import score_answer_support as cli

    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(storage_root=str(tmp_path)))
    path = tmp_path / "answers.json"
    path.write_text(json.dumps([example().model_dump()]))
    monkeypatch.setattr("sys.argv", ["score", str(path), "--provisional"])
    assert cli.main() == 0
    output = capsys.readouterr().out
    assert "Streamco" not in output
    assert json.loads(output)["pending"] == 2
    path.write_text('[{"private": "PRIVATE_SENTINEL"}]')
    with pytest.raises(SystemExit):
        cli.main()
    assert "PRIVATE_SENTINEL" not in capsys.readouterr().err
    monkeypatch.setattr("sys.argv", ["score", str(tmp_path.parent / "outside.json")])
    with pytest.raises(SystemExit):
        cli.main()
    assert "storage_root" in capsys.readouterr().err


async def test_project_chat_capture_uses_exact_stub_context(client, worker_session, monkeypatch):
    from apps.api import answer_support
    from apps.api.routers import chat
    from packages.core.schemas import ChatRequest
    from packages.llm.provider import StubProvider
    from tests.test_chat_skill_gaps import _owner, _requirements, _target

    profile, company = await _owner(worker_session)
    await _target(
        worker_session,
        profile,
        company,
        "Data Engineer",
        "Kafka tooling",
        _requirements(preferred=["kafka"]),
    )
    await worker_session.commit()
    ready = await chat.prepare(ChatRequest(message="jobs using kafka"), worker_session)
    assert isinstance(ready, chat.Ready)
    provider = StubProvider()
    answer = await provider.complete(chat.SYSTEM, ready.prompt)
    reply = chat.reply_for(ready, "stub", answer)
    record = answer_support.capture("project-kafka", ready, reply)
    assert record.prompt == ready.prompt
    assert record.answer == reply.reply
    assert "preferred" in record.prompt.lower()
    assert record.model == "stub"
    assert json.loads(record.model_dump_json())["snapshot_sha256"]
