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


@pytest.mark.parametrize("skill", ["python", "go"])
def test_and_or_is_an_alternative_without_weakening_a_separate_conjunction(skill):
    assert is_option("Solid programming skills in Python and/or Golang", BY_KEY[skill])
    assert is_option("Solid programming skills in Python and / or Golang", BY_KEY[skill])
    assert not is_option("Strong Python and Golang are required", BY_KEY[skill])


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


def test_long_whitespace_is_not_an_optional_skill_clause():
    import subprocess
    import sys

    # Bound the regression externally so the former quadratic regex cannot
    # hang the test runner. No network, models or database are involved.
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from packages.matching.evidence import is_option; "
            "from packages.matching.skill_vocab import BY_KEY; "
            "assert not is_option('Python' + ' ' * 100000 + 'required', BY_KEY['python']); "
            "assert is_option('Python' + ' ' * 100000 + 'or Go', BY_KEY['python']); "
            "assert is_option('Python /' + ' ' * 100000 + 'Go', BY_KEY['python']); "
            "assert is_option('Go' + ' ' * 100000 + '/ Python', BY_KEY['python']); "
            "assert not is_option('Python /' + ' ' * 100000 + '!', BY_KEY['python'])",
        ],
        check=True,
        timeout=5,
    )


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Kafka \t/\n Kinesis", True),
        ("Kinesis \u00a0/ \t Kafka", True),
        ("Kafka / 123", False),
        ("Kafka /é", False),
        ("é / Kafka", False),
        ("Kafka /", False),
        ("/ Kafka", False),
    ],
)
def test_slash_choices_preserve_whitespace_and_ascii_name_boundaries(line, expected):
    assert is_option(line, KAFKA) is expected


# --- version 6: category lists and "or similar" lists ------------------------------
#
# Measured on the live corpus (2026-11-22): 293 of 374 direct GCP requirements
# were lines like "public cloud platforms (AWS, GCP, Azure)", so a recurring
# gap of "GCP — 201 jobs" counted jobs that accept any one cloud.


@pytest.mark.parametrize(
    "line,skill_key",
    [
        (
            "At least 2 years of experience with public cloud platforms (AWS, GCP, Azure), "
            "cloud-based productivity tools, or virtualization technologies",
            "gcp",
        ),
        ("Knowledge of cloud platforms (AWS, Azure, Google Cloud)", "azure"),
        ("Modern web frameworks (React, Angular, Vue, Svelte)", "vue"),
        ("RESTful web services and Relational Databases (Postgres, SQL Server)", "postgresql"),
        ("Experience with monitoring and logging tools (Prometheus, Grafana, ELK)", "grafana"),
        (
            "Experience with software development or scripting in Linux, Bash, Python, C, "
            "C++, Java, Go, or similar technologies",
            "linux",
        ),
        (
            "Hands-on experience with Databricks, Apache Spark and lakehouse technologies, "
            "or comparable distributed data platforms",
            "databricks",
        ),
        ("Advanced knowledge of Snowflake, Kafka (or similar streaming platforms)", "kafka"),
        ("Experience with React, Node.js, TypeScript, or similar", "react"),
    ],
)
def test_a_category_list_or_an_or_similar_list_offers_each_as_a_choice(line, skill_key):
    assert is_option(line, BY_KEY[skill_key]), line


@pytest.mark.parametrize(
    "line,skill_key",
    [
        # A bracketed "or equivalent" belongs to the item right before it.
        (
            "Deep SQL expertise and production experience with Snowflake and dbt (or equivalent)",
            "sql",
        ),
        (
            "Deep SQL expertise and production experience with Snowflake and dbt (or equivalent)",
            "snowflake",
        ),
        ("Fluency in SQL and Python (or equivalent) for analysis", "sql"),
        ("Advanced knowledge of Snowflake, Kafka (or similar streaming platforms)", "snowflake"),
        # The "or similar" ends a phrase about other tools.
        (
            "Strong proficiency in Excel and PowerPoint, with experience and comfort in using "
            "Python, SQL, R or similar programming tools",
            "excel",
        ),
        (
            "Proficiency in C++, Python and ML frameworks like LangChain, LangSmith, "
            "or other AI agentic tools",
            "cpp",
        ),
        # Named on its own; the "or similar" is the cloud list's.
        (
            "Experience with containerized environments (Kubernetes), and public cloud "
            "platforms (AWS, GCP, or similar)",
            "kubernetes",
        ),
        # A stack, joined by "and".
        ("Solid understanding of databases (Postgres, Redis and ClickHouse)", "postgresql"),
        # A suite's parts, not a category's members.
        (
            "Advanced proficiency in Microsoft Office Suite (Word, Excel, PowerPoint, Outlook)",
            "excel",
        ),
        # "services" describes the team's stack.
        ("Full-stack proficiency spanning backend services (Java, Spring Boot, Kafka)", "java"),
        # One item is a name, not a list of choices.
        ("Experience with cloud platforms (AWS)", "aws"),
        # Required outright as well as listed.
        ("AWS is required; experience with cloud platforms (AWS, GCP, Azure)", "aws"),
    ],
)
def test_a_skill_asked_for_outright_stays_required(line, skill_key):
    assert not is_option(line, BY_KEY[skill_key]), line


def test_an_extracted_cloud_list_is_no_longer_a_direct_requirement_of_each_cloud():
    from packages.matching.requirements import extract

    description = (
        "Requirements\n"
        "At least 2 years of experience with public cloud platforms (AWS, GCP, Azure)\n"
        "Python is required"
    )
    required = {
        entry["skill"]: entry["option"]
        for entry in extract(description).as_json()["skills"]["required"]
    }

    assert required == {"aws": True, "gcp": True, "azure": True, "python": False}


def test_a_long_list_is_read_in_linear_time():
    import time

    line = "Experience with Kafka" + ", tool" * 5000 + " or similar"
    started = time.perf_counter()
    is_option(line, KAFKA)
    assert time.perf_counter() - started < 0.5
