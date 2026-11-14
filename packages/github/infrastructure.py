"""What a repository's own container and CI files declare, named the way a résumé names it.

Added 2026-11-14 for the Resume Studio, at the owner's asking: a Dockerfile,
a compose file and a GitHub Actions workflow are files the project runs, so
what they declare is a fact about the project with the same standing as a
package in its `requirements.txt` (`manifests.py`, CLAUDE.md §2.1).

The same two limits keep it evidence rather than a guess:

- **Only what the file declares.** A Dockerfile with a `FROM` line declares
  Docker. A compose file's `image:` and a workflow's `uses:` name the image
  and the action. Comments and `run:` scripts are not read: a shell line can
  mention a tool the project only checks for.
- **Only rows in the tables below.** An image or action missing from them is
  ignored, never turned into a name by rule. Adding a row is a decision about
  what may appear on a résumé; make it deliberately.

A README is not read, here or anywhere: it names tools the author compared
against, which is prose and not a declaration (`manifests.py`).
"""

from __future__ import annotations

import re

from packages.github.manifests import is_project_path

_DOCKERFILE_RE = re.compile(r"^(?:Dockerfile|Containerfile)(?:\.[\w.-]+)?$|^[\w.-]+\.Dockerfile$")
_COMPOSE_NAMES = frozenset(
    {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"}
)
_WORKFLOW_RE = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$")

_FROM_RE = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)", re.IGNORECASE | re.MULTILINE)
_IMAGE_RE = re.compile(r"^\s*(?:-\s*)?image:\s*[\"']?([^\s\"'#]+)", re.MULTILINE)
_USES_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*[\"']?([^\s\"'#@]+)", re.MULTILINE)
_SERVICES_RE = re.compile(r"^services:\s*$", re.MULTILINE)
_JOBS_RE = re.compile(r"^jobs:\s*$", re.MULTILINE)

#: Registries an image may be prefixed with. Dropped before the lookup.
_REGISTRIES = ("docker.io/", "ghcr.io/", "quay.io/", "public.ecr.aws/", "library/")

#: Image repository to the name a résumé uses.
IMAGE_NAMES: dict[str, str] = {
    "postgres": "PostgreSQL",
    "bitnami/postgresql": "PostgreSQL",
    "pgvector/pgvector": "pgvector",
    "ankane/pgvector": "pgvector",
    "mysql": "MySQL",
    "mariadb": "MariaDB",
    "mongo": "MongoDB",
    "redis": "Redis",
    "redis/redis-stack": "Redis",
    "rabbitmq": "RabbitMQ",
    "confluentinc/cp-kafka": "Apache Kafka",
    "bitnami/kafka": "Apache Kafka",
    "apache/kafka": "Apache Kafka",
    "elasticsearch": "Elasticsearch",
    "qdrant/qdrant": "Qdrant",
    "nginx": "Nginx",
    "traefik": "Traefik",
    "grafana/grafana": "Grafana",
    "prom/prometheus": "Prometheus",
    "minio/minio": "MinIO",
    "ollama/ollama": "Ollama",
    "nvidia/cuda": "CUDA",
    "apache/airflow": "Apache Airflow",
    "apache/spark": "Apache Spark",
}

#: Action, or the owner of a family of actions (ending in `/`), to a résumé name.
ACTION_NAMES: dict[str, str] = {
    "aws-actions/": "AWS",
    "google-github-actions/": "Google Cloud",
    "azure/login": "Azure",
    "azure/webapps-deploy": "Azure",
    "azure/setup-kubectl": "Kubernetes",
    "azure/k8s-deploy": "Kubernetes",
    "azure/setup-helm": "Helm",
    "docker/": "Docker",
    "hashicorp/setup-terraform": "Terraform",
    "pulumi/actions": "Pulumi",
    "astral-sh/setup-uv": "uv",
}


def is_infrastructure_path(path: str) -> bool:
    """Whether `path` (repository-relative) is a container or CI file of the project."""
    if _WORKFLOW_RE.match(path):
        return True
    if not is_project_path(path):
        return False
    name = path.rsplit("/", 1)[-1]
    return name in _COMPOSE_NAMES or bool(_DOCKERFILE_RE.match(name))


def _image_name(reference: str) -> str | None:
    """The résumé name for an image reference, or None when it is not in the table."""
    repository = reference.lower().split("@", 1)[0]
    for registry in _REGISTRIES:
        repository = repository.removeprefix(registry)
    # A tag follows the last colon; a registry port would sit before a slash.
    head, _, tail = repository.rpartition(":")
    if head and "/" not in tail:
        repository = head
    return IMAGE_NAMES.get(repository)


def _action_name(action: str) -> str | None:
    lowered = action.lower()
    for prefix, name in ACTION_NAMES.items():
        matches = lowered.startswith(prefix) if prefix.endswith("/") else lowered == prefix
        if matches:
            return name
    return None


def _images(text: str, pattern: re.Pattern[str]) -> set[str]:
    return {name for name in map(_image_name, pattern.findall(text)) if name}


def declared_in(path: str, text: str) -> set[str]:
    """Résumé names for what one container or CI file declares. Unknown names drop out."""
    if _WORKFLOW_RE.match(path):
        if not _JOBS_RE.search(text):
            return set()
        actions = {name for name in map(_action_name, _USES_RE.findall(text)) if name}
        return {"GitHub Actions"} | actions
    name = path.rsplit("/", 1)[-1]
    if name in _COMPOSE_NAMES:
        if not _SERVICES_RE.search(text):
            return set()
        return {"Docker", "Docker Compose"} | _images(text, _IMAGE_RE)
    if _DOCKERFILE_RE.match(name):
        if not _FROM_RE.search(text):
            return set()
        return {"Docker"} | _images(text, _FROM_RE)
    return set()
