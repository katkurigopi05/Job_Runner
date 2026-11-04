"""A posting that contains a skill's name has not thereby asked for it.

"Which jobs use Kafka?" was answered from excerpts, and an excerpt shows the
word whether the posting requires it, prefers it, offers it as one of two
queues, or mentions having left it. `requirements.py` already knows the first
two apart, by heading; this is the reading handed to the model, and the one
thing added to it.
"""

from __future__ import annotations

import pytest

from packages.matching.evidence import Strength, is_option, skill_evidence
from packages.matching.skill_vocab import BY_KEY

KAFKA = BY_KEY["kafka"]
PYTHON = BY_KEY["python"]


def _requirements(**by_strength: tuple[str, str]) -> dict:
    """`required=("kafka", "the line")` and so on, as `requirements.py` stores them."""
    skills: dict[str, list[dict[str, str]]] = {
        "required": [],
        "preferred": [],
        "unclassified": [],
    }
    for strength, (skill, quote) in by_strength.items():
        skills[strength].append({"skill": skill, "label": BY_KEY[skill].label, "quote": quote})
    return {"skills": skills}


@pytest.mark.parametrize(
    "line",
    [
        "Experience with Kafka or Kinesis",
        "Experience with Kinesis or Kafka",
        "A streaming system: Kafka, Kinesis, or Pub/Sub",
        "Pulsar, Kafka or similar",
        "Kafka (or an equivalent message bus)",
        "Kafka/Kinesis in production",
        "Kinesis/Kafka in production",
        "A message queue such as Kafka",
        "Message queues (e.g. RabbitMQ, Kafka)",
        "An event log like Kafka",
    ],
)
def test_a_line_that_offers_it_among_others_is_an_option(line) -> None:
    assert is_option(line, KAFKA), line


@pytest.mark.parametrize(
    "line",
    [
        "Strong experience with Kafka",
        "Python and Kafka in production",
        "Kafka, Flink and Spark",
        "Python or Go required; Kafka a plus",
        "Kafka. Or tell us what you would use instead.",
        "Operate Kafka for teams building CI/CD pipelines",
        "Tools including Kafka and Spark",
    ],
)
def test_a_line_that_asks_for_it_outright_is_not(line) -> None:
    assert not is_option(line, KAFKA), line


def test_the_other_half_of_that_line_is_the_option() -> None:
    """One line, two skills, and only one of them is offered as a choice."""
    line = "Python or Go required; Kafka a plus"

    assert is_option(line, PYTHON) and not is_option(line, KAFKA)


@pytest.mark.parametrize("skill_key", ["pytorch", "tensorflow"])
@pytest.mark.parametrize(
    "line",
    [
        "Experience with PyTorch 2.0 or TensorFlow 2.1",
        "Experience with TensorFlow 2.1 or PyTorch 2.0",
        "Experience with PyTorch v2.0+ or TensorFlow 2.x",
    ],
)
def test_versioned_frameworks_can_be_alternatives(line: str, skill_key: str) -> None:
    assert is_option(line, BY_KEY[skill_key])


@pytest.mark.parametrize("skill_key", ["pytorch", "tensorflow"])
def test_versioned_frameworks_joined_by_and_are_not_alternatives(skill_key: str) -> None:
    assert not is_option("Experience with PyTorch 2.0 and TensorFlow 2.1", BY_KEY[skill_key])


@pytest.mark.parametrize(
    ("line", "skill_key"),
    [
        ("PyTorch 2.0. Or explain another approach.", "pytorch"),
        ("Kafka. Or tell us what you would use instead.", "kafka"),
        ("Operate Kafka for teams building CI/CD pipelines", "kafka"),
        ("Build CI/CD pipelines using Kafka", "cicd"),
    ],
)
def test_version_handling_preserves_sentence_and_technology_boundaries(
    line: str, skill_key: str
) -> None:
    assert not is_option(line, BY_KEY[skill_key])


def test_required_is_said_with_the_line_it_was_read_from() -> None:
    found = skill_evidence(_requirements(required=("kafka", "5+ years running Kafka")), "kafka")

    assert found.strength is Strength.REQUIRED and not found.option
    assert found.in_words() == 'Kafka here: required — "5+ years running Kafka"'


def test_required_as_one_of_two_is_not_required_of_that_one() -> None:
    """Under "Requirements", and either will do."""
    found = skill_evidence(
        _requirements(required=("kafka", "Experience with Kafka or Kinesis")), "kafka"
    )

    assert found.strength is Strength.REQUIRED and found.option
    assert "one of several options, so not this one specifically" in found.in_words()
    assert '"Experience with Kafka or Kinesis"' in found.in_words()


def test_preferred_is_not_required() -> None:
    found = skill_evidence(_requirements(preferred=("kafka", "Bonus: Kafka")), "kafka")

    assert found.strength is Strength.PREFERRED
    assert "preferred, not required" in found.in_words()


def test_named_under_no_heading_is_not_shown_to_be_asked_for() -> None:
    """ "we build with Go" in a paragraph about the company (`requirements.py`)."""
    found = skill_evidence(
        _requirements(unclassified=("kafka", "We moved off Kafka last year")), "kafka"
    )

    assert found.strength is Strength.NAMED
    assert "not shown to be asked for" in found.in_words()


def test_a_skill_the_posting_was_not_read_as_naming_says_so() -> None:
    found = skill_evidence(_requirements(required=("python", "Python")), "kafka")

    assert found.strength is Strength.UNREAD and found.quote is None
    assert "not among the skills read" in found.in_words()


def test_a_posting_nobody_has_read_is_not_one_that_asks_for_nothing() -> None:
    assert skill_evidence(None, "kafka") is None


def test_the_strongest_stored_reading_is_the_one_given() -> None:
    """`requirements.py` stores a skill once; if a row ever held it twice, required wins."""
    requirements = _requirements(required=("kafka", "Kafka"), preferred=("kafka", "Kafka a plus"))

    assert skill_evidence(requirements, "kafka").strength is Strength.REQUIRED


def test_alternative_beyond_quote_limit_is_still_an_option():
    from packages.matching.requirements import extract

    # A concise list with a long qualifier is still an alternative to Kafka.
    line = "Kafka (" + "production systems " * 20 + ") or Kinesis"
    found = skill_evidence(extract("Requirements\n" + line).as_json(), "kafka")
    assert len(found.quote) <= 240
    assert found.option is True


@pytest.mark.parametrize("skill_key", ["pytorch", "tensorflow"])
@pytest.mark.parametrize(
    "line",
    [
        "Familiarity with at least one ML framework (pytorch, tensorflow etc)",
        "Experience with one or more deep-learning frameworks (PyTorch, TensorFlow)",
        "Proficiency in one of the following frameworks (TensorFlow, PyTorch)",
    ],
)
def test_quantified_framework_choices_do_not_require_each_named_framework(line, skill_key):
    from packages.matching.requirements import extract

    found = skill_evidence(extract("Requirements\n" + line).as_json(), skill_key)
    assert found.strength is Strength.REQUIRED
    assert found.option is True


@pytest.mark.parametrize(
    "line",
    [
        "Familiarity with PyTorch",
        "Familiarity with at least one ML framework (PyTorch)",
        "Use PyTorch and TensorFlow",
        "Expertise in tools including but not limited to PyTorch, Ray, vLLM",
        "At least one project (TensorFlow, Python); PyTorch proficiency is required",
        "At least one ML framework (TensorFlow, Python); PyTorch proficiency is required",
    ],
)
def test_quantified_choice_rule_does_not_weaken_direct_pytorch_evidence(line):
    assert not is_option(line, BY_KEY["pytorch"])


@pytest.mark.parametrize("direct_first", [False, True])
def test_direct_pytorch_demand_outranks_quantified_framework_choice(direct_first):
    from packages.matching.requirements import extract

    option = "Familiarity with at least one ML framework (pytorch, tensorflow etc)"
    direct = "Proficiency with PyTorch is required"
    lines = [direct, option] if direct_first else [option, direct]
    found = skill_evidence(extract("Requirements\n" + "\n".join(lines)).as_json(), "pytorch")
    assert found.option is False
    assert found.quote == direct


def test_version_three_quantified_framework_reading_is_refreshed_without_mutation():
    from packages.matching.required_skills import demand_class, fresh_requirements
    from packages.matching.requirements import EXTRACTOR_VERSION, extract

    description = (
        "Requirements\nFamiliarity with at least one ML framework (pytorch, tensorflow etc)"
    )
    legacy = extract(description).as_json()
    legacy["version"] = 3
    pytorch = next(item for item in legacy["skills"]["required"] if item["skill"] == "pytorch")
    pytorch["option"] = False

    current = fresh_requirements(legacy, 3, description)

    assert EXTRACTOR_VERSION > 3
    assert current["version"] == EXTRACTOR_VERSION
    assert demand_class(current, BY_KEY["pytorch"])[0] == "options"
    assert legacy["version"] == 3 and pytorch["option"] is False


@pytest.mark.parametrize(
    "line",
    [
        "Proficiency in PyTorch and at least one ML framework (PyTorch, TensorFlow)",
        "Familiarity with at least one ML framework (PyTorch, TensorFlow) "
        "and proficiency in PyTorch",
    ],
)
def test_same_clause_direct_demand_survives_quantified_framework_choice(line):
    from packages.matching.requirements import extract

    reading = extract("Requirements\n" + line).as_json()
    assert skill_evidence(reading, "pytorch").option is False
    assert skill_evidence(reading, "tensorflow").option is True
