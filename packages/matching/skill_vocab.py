"""The technologies a posting can be read as asking for.

Curated rather than learned, for the reason `roles.py` gives for its title
table: a vocabulary inferred from the corpus is a vocabulary of whatever the
corpus happens to repeat, and the fields a filter keys on have to mean the
same thing next month.

Two rules keep false matches out, because a filter excludes on these:

- **Ambiguous English words are matched case-sensitively.** "react to an
  incident" is not React; "Go" in "go-to-market" is not Go. The entries that
  need it say so.
- **Single-letter languages are not listed.** `C` and `R` appear as initials,
  grades and list markers far more often than as languages; `C++` and `C#`
  are listed because their spelling is unambiguous.

Adding an entry is safe. Loosening a case-sensitive one is not — measure it
against `tests/fixtures/golden/` first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Characters that continue a technology token, so `C++` is not `C` and
#: `Node.js` is not `Node`.
_TOKEN = r"A-Za-z0-9+#"
_LEFT = rf"(?<![{_TOKEN}])"
_RIGHT = rf"(?![{_TOKEN}]|\.[A-Za-z])"


@dataclass(frozen=True)
class Skill:
    key: str
    label: str
    pattern: re.Pattern[str]


def _skill(key: str, label: str, *spellings: str, case_sensitive: bool = False) -> Skill:
    alternation = "|".join(spellings or (re.escape(label),))
    flags = 0 if case_sensitive else re.IGNORECASE
    return Skill(key, label, re.compile(rf"{_LEFT}(?:{alternation}){_RIGHT}", flags))


SKILLS: tuple[Skill, ...] = (
    # Languages
    _skill("python", "Python"),
    _skill("java", "Java", r"Java(?!\s*Script)"),
    _skill("javascript", "JavaScript", r"JavaScript", r"ECMAScript"),
    _skill("typescript", "TypeScript"),
    _skill("go", "Go", r"Golang", r"Go(?=\s*(?:,|/|\)|and\s+[A-Z]))", case_sensitive=True),
    _skill("rust", "Rust", r"Rust", case_sensitive=True),
    _skill("cpp", "C++", r"C\+\+"),
    _skill("csharp", "C#", r"C#", r"\.NET", r"dotnet"),
    _skill("ruby", "Ruby", r"Ruby", case_sensitive=True),
    _skill("php", "PHP"),
    _skill("scala", "Scala", r"Scala", case_sensitive=True),
    _skill("kotlin", "Kotlin"),
    _skill("swift", "Swift", r"Swift", case_sensitive=True),
    _skill("sql", "SQL", r"SQL"),
    _skill("bash", "Bash / shell", r"Bash", r"shell scripting"),
    # Web and APIs
    _skill("react", "React", r"React(?:\.js|JS)?", case_sensitive=True),
    _skill("angular", "Angular", r"Angular", case_sensitive=True),
    _skill("vue", "Vue", r"Vue(?:\.js)?", case_sensitive=True),
    _skill("nextjs", "Next.js", r"Next\.js", r"NextJS"),
    _skill("nodejs", "Node.js", r"Node\.js", r"NodeJS", r"Node(?=\s*(?:,|/))"),
    _skill("django", "Django"),
    _skill("flask", "Flask", r"Flask", case_sensitive=True),
    _skill("fastapi", "FastAPI"),
    _skill("spring", "Spring", r"Spring Boot", r"Spring(?=\s*(?:,|/|\)))", case_sensitive=True),
    _skill("rails", "Ruby on Rails", r"Ruby on Rails", r"Rails", case_sensitive=True),
    _skill("graphql", "GraphQL"),
    _skill("rest", "REST APIs", r"REST(?:ful)?(?:\s+APIs?)?", case_sensitive=True),
    _skill("grpc", "gRPC"),
    # Data stores and pipelines
    _skill("postgresql", "PostgreSQL", r"PostgreSQL", r"Postgres"),
    _skill("mysql", "MySQL"),
    _skill("mongodb", "MongoDB", r"MongoDB", r"Mongo"),
    _skill("redis", "Redis"),
    _skill("elasticsearch", "Elasticsearch", r"Elasticsearch", r"OpenSearch"),
    _skill("cassandra", "Cassandra"),
    _skill("dynamodb", "DynamoDB"),
    _skill("kafka", "Kafka"),
    _skill("spark", "Spark", r"Apache Spark", r"PySpark", r"Spark", case_sensitive=True),
    _skill("airflow", "Airflow", r"Airflow", case_sensitive=True),
    _skill("dbt", "dbt", r"dbt", case_sensitive=True),
    _skill("snowflake", "Snowflake", r"Snowflake", case_sensitive=True),
    _skill("bigquery", "BigQuery"),
    _skill("redshift", "Redshift"),
    _skill("databricks", "Databricks"),
    _skill("hadoop", "Hadoop"),
    _skill("pandas", "pandas", r"pandas"),
    _skill("numpy", "NumPy"),
    _skill("tableau", "Tableau"),
    _skill("looker", "Looker", r"Looker", case_sensitive=True),
    _skill("powerbi", "Power BI", r"Power\s?BI"),
    _skill("excel", "Excel", r"Excel", case_sensitive=True),
    # Machine learning
    _skill("pytorch", "PyTorch"),
    _skill("tensorflow", "TensorFlow"),
    _skill("scikit_learn", "scikit-learn", r"scikit-learn", r"sklearn"),
    _skill("keras", "Keras"),
    _skill("huggingface", "Hugging Face", r"Hugging\s?Face"),
    _skill("machine_learning", "Machine learning", r"machine learning", r"ML(?=\s|,|/)"),
    _skill("deep_learning", "Deep learning", r"deep learning"),
    _skill("nlp", "NLP", r"NLP", r"natural language processing"),
    _skill("computer_vision", "Computer vision", r"computer vision"),
    _skill("llm", "LLMs", r"LLMs?", r"large language models?"),
    # Cloud, infrastructure, operations
    _skill("aws", "AWS", r"AWS", r"Amazon Web Services"),
    _skill("gcp", "GCP", r"GCP", r"Google Cloud(?: Platform)?"),
    _skill("azure", "Azure", r"Azure", case_sensitive=True),
    _skill("kubernetes", "Kubernetes", r"Kubernetes", r"K8s"),
    _skill("docker", "Docker"),
    _skill("terraform", "Terraform"),
    _skill("ansible", "Ansible"),
    _skill("cicd", "CI/CD", r"CI/CD", r"continuous integration"),
    _skill("jenkins", "Jenkins"),
    _skill("github_actions", "GitHub Actions"),
    _skill("linux", "Linux"),
    _skill("prometheus", "Prometheus"),
    _skill("grafana", "Grafana"),
    _skill("datadog", "Datadog", r"Datadog", case_sensitive=True),
    _skill("git", "Git", r"Git(?!Hub|Lab)", case_sensitive=True),
    # Mobile and design
    _skill("ios", "iOS", r"iOS", case_sensitive=True),
    _skill("android", "Android", r"Android", case_sensitive=True),
    _skill("figma", "Figma"),
)

BY_KEY: dict[str, Skill] = {skill.key: skill for skill in SKILLS}


def find_skills(line: str) -> list[Skill]:
    """Every vocabulary skill named in `line`, in vocabulary order, once each."""
    return [skill for skill in SKILLS if skill.pattern.search(line)]


def normalize_skill(term: str) -> str | None:
    """The vocabulary key for something a person typed into a filter, or None.

    Matched against labels and spellings the same way a posting is, so a
    filter for "postgres" and a posting saying "PostgreSQL" meet on one key.
    """
    cleaned = term.strip()
    if not cleaned:
        return None
    if cleaned.lower() in BY_KEY:
        return cleaned.lower()
    for skill in SKILLS:
        if skill.label.lower() == cleaned.lower():
            return skill.key
    # Typed input is often lowercase, so case-sensitive spellings are retried
    # against the label's casing rather than refused.
    for skill in SKILLS:
        if skill.pattern.fullmatch(cleaned) or re.fullmatch(
            skill.pattern.pattern, cleaned, re.IGNORECASE
        ):
            return skill.key
    return None


__all__ = ["BY_KEY", "SKILLS", "Skill", "find_skills", "normalize_skill"]
