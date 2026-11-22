"""Conservative, static evidence rules. Repository text is data and never executed."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from packages.github.infrastructure import declared_in, is_infrastructure_path
from packages.github.manifests import is_manifest_path, packages_in, technologies
from packages.matching.skill_vocab import CODE_SKILLS, find_skills


class Classification(StrEnum):
    DECLARED = "declared"
    IMPLEMENTED = "implemented"
    DOCUMENTED = "documented"
    INSUFFICIENT = "insufficient"


@dataclass(frozen=True)
class Signal:
    skills: tuple[str, ...]
    classification: Classification
    start: int
    end: int
    explanation: str
    wording: tuple[str, ...] = ()


def _signal(
    skills: tuple[str, ...], start: int, end: int, explanation: str, *wording: str
) -> Signal:
    return Signal(skills, Classification.IMPLEMENTED, start, end, explanation, wording)


def _active(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Skip stubs, docstrings, and functions that only raise or return null."""
    return any(
        not isinstance(node, (ast.Pass, ast.Raise))
        and not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
        and not (
            isinstance(node, ast.Return)
            and (node.value is None or isinstance(node.value, ast.Constant))
        )
        for node in function.body
    )


def _python(code: str) -> Iterator[Signal]:
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError):
        return

    bound = {
        alias.asname or alias.name: alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "fastapi" and node.level == 0
        for alias in node.names
        if alias.name in {"FastAPI", "APIRouter"}
    }
    modules = {
        alias.asname or alias.name: alias.name
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name in {"fastapi", "boto3"}
    }
    routers: set[str] = set()
    clients: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        constructor = node.value.func
        names = {target.id for target in node.targets if isinstance(target, ast.Name)}
        if isinstance(constructor, ast.Name) and constructor.id in bound:
            routers.update(names)
        if isinstance(constructor, ast.Attribute) and isinstance(constructor.value, ast.Name):
            module = modules.get(constructor.value.id)
            if module == "fastapi" and constructor.attr in {"FastAPI", "APIRouter"}:
                routers.update(names)
            if (
                module == "boto3"
                and constructor.attr == "client"
                and node.value.args
                and isinstance(node.value.args[0], ast.Constant)
                and isinstance(node.value.args[0].value, str)
            ):
                clients.update(names)

    functions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _active(node)
    ]
    if functions:
        node = functions[0]
        yield _signal(
            ("python",),
            node.lineno,
            node.end_lineno or node.lineno,
            "A Python function contains executable logic.",
            "Implemented project logic in Python.",
            "Wrote Python code for the project.",
        )

    for node in functions:
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Name)
                and decorator.func.value.id in routers
                and decorator.func.attr in {"get", "post", "put", "patch", "delete", "head"}
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
                and isinstance(decorator.args[0].value, str)
                and decorator.args[0].value.startswith("/")
            ):
                yield _signal(
                    ("fastapi", "rest"),
                    decorator.lineno,
                    node.end_lineno or node.lineno,
                    "An HTTP route is bound to a FastAPI app or router and has a handler body. "
                    "REST design constraints and deployment are unverified.",
                    "Implemented HTTP API routes with FastAPI.",
                    "Defined FastAPI routes and their request handlers.",
                )
                break

        args = [argument.arg for argument in (*node.args.posonlyargs, *node.args.args)]
        # Require a recognizably AWS-shaped response and a conventional signature.
        if (
            node.name == "lambda_handler"
            and args[:2] == ["event", "context"]
            and any(
                isinstance(child, ast.Return)
                and isinstance(child.value, ast.Dict)
                and {"statusCode", "body"}
                <= {
                    key.value
                    for key in child.value.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                }
                for child in node.body
            )
        ):
            yield _signal(
                ("aws_lambda", "aws", "python"),
                node.lineno,
                node.end_lineno or node.lineno,
                "A Python Lambda handler accepts an event and context and returns an "
                "HTTP-style Lambda response.",
                "Implemented a Python handler for AWS Lambda.",
                "Wrote an AWS Lambda handler in Python.",
            )

        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id in clients
                and call.func.attr
                in {
                    "get_object",
                    "put_object",
                    "list_objects_v2",
                    "get_item",
                    "put_item",
                    "send_message",
                    "receive_message",
                    "invoke",
                    "publish",
                }
            ):
                yield _signal(
                    ("aws", "python"),
                    call.lineno,
                    call.end_lineno or call.lineno,
                    "Python code calls an AWS service operation through a boto3 client.",
                    "Integrated an AWS service through the boto3 SDK.",
                    "Called AWS service APIs from Python using boto3.",
                )
                break


def _fields(node: Node | None) -> dict[str, Node]:
    if not isinstance(node, MappingNode):
        return {}
    fields = {key.value: value for key, value in node.value if isinstance(key, ScalarNode)}
    return fields if len(fields) == len(node.value) else {}


def _scalar(node: Node | None) -> str:
    return node.value if isinstance(node, ScalarNode) else ""


def _yaml(path: str, code: str) -> Iterator[Signal]:
    # Compose nodes without constructing Python values. Alias references are not
    # traversed recursively and duplicate mapping keys are rejected.
    try:
        documents = list(yaml.compose_all(code, Loader=yaml.SafeLoader))
    except (yaml.YAMLError, RecursionError, ValueError):
        return
    for doc in documents:
        fields = _fields(doc)
        jobs = fields.get("jobs")
        if path.startswith(".github/workflows/") and "on" in fields and jobs is not None:
            for job in _fields(jobs).values():
                steps = _fields(job).get("steps")
                if not isinstance(steps, SequenceNode):
                    continue
                commands = [_scalar(_fields(step).get("run")) for step in steps.value]
                if any(command.strip() for command in commands):
                    yield _signal(
                        ("github_actions",),
                        job.start_mark.line + 1,
                        job.end_mark.line,
                        "A triggered GitHub Actions workflow defines a job with run steps.",
                        "Configured a GitHub Actions workflow for project automation.",
                        "Automated project tasks with GitHub Actions.",
                    )
                if any(
                    re.search(
                        r"(?m)^\s*(?:(?:python -m |uv run )?(?:pytest|ruff)\b|"
                        r"(?:npm|pnpm|yarn) (?:run )?(?:test|lint|build)\b|"
                        r"(?:go test|cargo test|mvn test|make (?:test|lint|build))\b)",
                        command,
                    )
                    for command in commands
                ):
                    yield _signal(
                        ("cicd", "github_actions"),
                        job.start_mark.line + 1,
                        job.end_mark.line,
                        "A GitHub Actions job runs a test, lint, or build command. "
                        "Workflow success and continuous deployment are unverified.",
                        "Configured continuous integration checks with GitHub Actions.",
                        "Added GitHub Actions checks to the project's CI workflow.",
                    )
                    break
        spec = fields.get("spec")
        kind = _scalar(fields.get("kind"))
        if spec is not None and kind in {"Deployment", "StatefulSet", "DaemonSet", "Job"}:
            version = _scalar(fields.get("apiVersion"))
            if version not in ({"batch/v1"} if kind == "Job" else {"apps/v1"}):
                continue
            template = _fields(_fields(spec).get("template"))
            containers = _fields(template.get("spec")).get("containers")
            if isinstance(containers, SequenceNode) and any(
                _scalar(_fields(item).get("name")) and _scalar(_fields(item).get("image"))
                for item in containers.value
            ):
                yield _signal(
                    ("kubernetes",),
                    doc.start_mark.line + 1,
                    doc.end_mark.line,
                    "A Kubernetes workload manifest specifies a container image and template. "
                    "A live rollout is unverified.",
                    "Configured a Kubernetes workload manifest.",
                    "Defined a container workload for Kubernetes.",
                )
        if path.rsplit("/", 1)[-1] in {
            "compose.yml",
            "compose.yaml",
            "docker-compose.yml",
            "docker-compose.yaml",
        }:
            for service in _fields(fields.get("services")).values():
                if _scalar(_fields(service).get("build")) or _fields(_fields(service).get("build")):
                    yield _signal(
                        ("docker",),
                        service.start_mark.line + 1,
                        service.end_mark.line,
                        "Docker Compose defines a service with a build configuration.",
                        "Configured a project service with Docker Compose.",
                        "Defined a Docker Compose service build.",
                    )
                    break

        for resource in _fields(fields.get("Resources")).values():
            definition = _fields(resource)
            properties = _fields(definition.get("Properties"))
            if (
                _scalar(definition.get("Type"))
                in {"AWS::Lambda::Function", "AWS::Serverless::Function"}
                and _scalar(properties.get("Handler"))
                and _scalar(properties.get("Runtime"))
                and any(key in properties for key in ("Code", "CodeUri", "InlineCode"))
            ):
                yield _signal(
                    ("aws_lambda", "aws"),
                    resource.start_mark.line + 1,
                    resource.end_mark.line,
                    "The AWS infrastructure definition configures a Lambda function and handler. "
                    "The handler implementation and deployment are separate checks.",
                    "Configured an AWS Lambda function in an infrastructure template.",
                    "Defined AWS Lambda infrastructure for the project.",
                )


def inspect_file(path: str, content: str) -> Iterator[Signal]:
    """Return fixed descriptions and line numbers, never raw source excerpts."""
    filename = path.rsplit("/", 1)[-1]
    lines = content.splitlines()
    if not lines:
        return
    if path.endswith(".py"):
        yield from _python(content)
    elif path.endswith((".yml", ".yaml")):
        yield from _yaml(path, content)
    elif is_infrastructure_path(path):
        base = next(
            (n for n, line in enumerate(lines, 1) if re.match(r"^\s*FROM\s+\S", line, re.I)), 0
        )
        action = next(
            (
                n
                for n, line in enumerate(lines, 1)
                if n > base and re.match(r"^\s*(?:COPY|RUN|CMD|ENTRYPOINT)\s+\S", line, re.I)
            ),
            0,
        )
        if base and action:
            yield _signal(
                ("docker",),
                base,
                action,
                "A Dockerfile selects a base image and defines an executable build or run step.",
                "Wrote a Dockerfile for the project.",
                "Defined a Docker image build for the project.",
            )
    elif filename.lower() in {"readme.md", "readme.rst", "readme.txt"}:
        for number, line in enumerate(lines, 1):
            skills = tuple(skill.key for skill in find_skills(line, CODE_SKILLS))
            if skills:
                denied = re.search(
                    r"\b(?:does not use|not (?:used|implemented|supported)|"
                    r"without|planned|roadmap|unsupported)\b",
                    line,
                    re.I,
                )
                yield Signal(
                    skills,
                    Classification.INSUFFICIENT if denied else Classification.DOCUMENTED,
                    number,
                    number,
                    "Documentation mentions this technology with a caveat."
                    if denied
                    else "Documentation mentions this technology.",
                )
    names = technologies(packages_in(path, content)) if is_manifest_path(path) else ()
    if is_infrastructure_path(path):
        names = sorted(declared_in(path, content))
    skills = tuple(
        dict.fromkeys(skill.key for name in names for skill in find_skills(name, CODE_SKILLS))
    )
    if skills:
        yield Signal(
            skills,
            Classification.DECLARED,
            1,
            len(lines),
            "A dependency or configuration file declares this technology.",
        )
