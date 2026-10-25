"""What a repository's own dependency files declare, named the way a résumé names it.

A package in a repository's `requirements.txt` or `package.json` is a fact
about that project, reported by the project itself. That makes it evidence a
tailored résumé may use for that project (CLAUDE.md §2.1), the same standing
as the repository's description and primary language.

Two things keep it evidence rather than a guess:

- **Only declared dependencies.** Not imports, not a README. A README names
  tools the author compared against, and an import in a notebook can be one
  cell in a tutorial. A dependency file is what the project installs.
- **Only packages in `TECHNOLOGY_NAMES`.** A package missing from the table
  is ignored, never turned into a name by rule. `left-pad` is not a skill, and
  a name built from a package id (`Google Generativeai`) is not one a
  recruiter searches for. Adding a row is a decision about what may appear on
  a résumé; make it deliberately.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterable

#: A dependency file anywhere in the tree, except where it is not the project's.
_REQUIREMENTS_RE = re.compile(r"^requirements[\w.-]*\.txt$")
_MANIFEST_NAMES = frozenset({"pyproject.toml", "package.json", "Cargo.toml"})
#: Directories whose manifests belong to something else: installed packages,
#: vendored code, build output, and test fixtures written to look like a
#: project.
_NOT_THE_PROJECT = frozenset(
    {
        "node_modules",
        "vendor",
        "third_party",
        ".venv",
        "venv",
        "site-packages",
        "dist",
        "build",
        "__pycache__",
        "fixtures",
    }
)

#: The leading name of a requirement line: `torch>=2`, `llama-index[openai]`.
_REQUIREMENT_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def is_manifest_path(path: str) -> bool:
    """Whether `path` (repository-relative) is a dependency file of the project."""
    parts = path.split("/")
    if set(parts[:-1]) & _NOT_THE_PROJECT:
        return False
    name = parts[-1]
    return name in _MANIFEST_NAMES or bool(_REQUIREMENTS_RE.match(name))


def _normalize(package: str) -> str:
    """`Scikit_Learn` and `scikit-learn` are one package (PEP 503, roughly)."""
    return re.sub(r"[_.]+", "-", package.strip().lower())


def _requirement_names(lines: Iterable[str]) -> set[str]:
    names: set[str] = set()
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = _REQUIREMENT_RE.match(line)
        if match:
            names.add(_normalize(match.group(1)))
    return names


def _pyproject(text: str) -> set[str]:
    data = tomllib.loads(text)
    project = data.get("project", {})
    lines: list[str] = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        lines.extend(group)
    for group in data.get("dependency-groups", {}).values():
        lines.extend(item for item in group if isinstance(item, str))
    names = _requirement_names(lines)

    poetry = data.get("tool", {}).get("poetry", {})
    tables = [poetry.get("dependencies", {}), poetry.get("dev-dependencies", {})]
    tables += [group.get("dependencies", {}) for group in poetry.get("group", {}).values()]
    for table in tables:
        names |= {_normalize(name) for name in table if name.lower() != "python"}
    return names


def _package_json(text: str) -> set[str]:
    data = json.loads(text)
    if not isinstance(data, dict):
        return set()
    names: set[str] = set()
    for key in ("dependencies", "devDependencies"):
        table = data.get(key)
        if isinstance(table, dict):
            names |= {name.lower() for name in table}
    return names


def _cargo(text: str) -> set[str]:
    data = tomllib.loads(text)
    tables = [data.get("dependencies", {}), data.get("dev-dependencies", {})]
    tables.append(data.get("workspace", {}).get("dependencies", {}))
    return {_normalize(name) for table in tables for name in table}


def packages_in(path: str, text: str) -> set[str]:
    """Package names a dependency file declares. A file that will not parse declares none."""
    name = path.rsplit("/", 1)[-1]
    try:
        if name == "pyproject.toml":
            return _pyproject(text)
        if name == "package.json":
            return _package_json(text)
        if name == "Cargo.toml":
            return _cargo(text)
        if _REQUIREMENTS_RE.match(name):
            return _requirement_names(text.splitlines())
    except (ValueError, TypeError, AttributeError):
        # tomllib.TOMLDecodeError and json.JSONDecodeError are ValueErrors.
        return set()
    return set()


#: Package to the name a résumé uses. Several packages may share a name.
#:
#: Only tools a person is credited with *using*: no utility libraries
#: (`requests`, `python-dotenv`), no type stubs, no transitive runtimes. Each
#: name is the one the tool calls itself, so it matches what a posting asks
#: for.
TECHNOLOGY_NAMES: dict[str, str] = {
    # Python: machine learning and AI
    "torch": "PyTorch",
    "torchvision": "PyTorch",
    "tensorflow": "TensorFlow",
    "keras": "Keras",
    "transformers": "Hugging Face Transformers",
    "sentence-transformers": "Sentence Transformers",
    "scikit-learn": "scikit-learn",
    "sklearn": "scikit-learn",
    "xgboost": "XGBoost",
    "lightgbm": "LightGBM",
    "spacy": "spaCy",
    "nltk": "NLTK",
    "gensim": "Gensim",
    "opencv-python": "OpenCV",
    "opencv-python-headless": "OpenCV",
    "mediapipe": "MediaPipe",
    "ultralytics": "YOLO (Ultralytics)",
    "librosa": "librosa",
    "prophet": "Prophet",
    "statsmodels": "statsmodels",
    "pandas": "pandas",
    "numpy": "NumPy",
    "scipy": "SciPy",
    "duckdb": "DuckDB",
    # Python: LLMs and retrieval
    "openai": "OpenAI API",
    "anthropic": "Anthropic API",
    "google-generativeai": "Gemini API",
    "google-genai": "Gemini API",
    "groq": "Groq",
    "ollama": "Ollama",
    "langchain": "LangChain",
    "langchain-core": "LangChain",
    "langchain-community": "LangChain",
    "langchain-openai": "LangChain",
    "langgraph": "LangGraph",
    "llama-index": "LlamaIndex",
    "llama-index-core": "LlamaIndex",
    "qdrant-client": "Qdrant",
    "elasticsearch": "Elasticsearch",
    "pgvector": "pgvector",
    "supabase": "Supabase",
    "mcp": "MCP",
    "fastmcp": "MCP",
    # Python: services, data and testing
    "fastapi": "FastAPI",
    "flask": "Flask",
    "django": "Django",
    "streamlit": "Streamlit",
    "pydantic": "Pydantic",
    "sqlalchemy": "SQLAlchemy",
    "alembic": "Alembic",
    "psycopg": "PostgreSQL",
    "psycopg2": "PostgreSQL",
    "psycopg2-binary": "PostgreSQL",
    "asyncpg": "PostgreSQL",
    "playwright": "Playwright",
    "selenium": "Selenium",
    "beautifulsoup4": "BeautifulSoup",
    "pytest": "pytest",
    "plotly": "Plotly",
    "matplotlib": "Matplotlib",
    "gurobipy": "Gurobi",
    "pyomo": "Pyomo",
    "pulp": "PuLP",
    "cvxpy": "CVXPY",
    "weasyprint": "WeasyPrint",
    "structlog": "structlog",
    # JavaScript and TypeScript
    "react": "React",
    "next": "Next.js",
    "vite": "Vite",
    "vitest": "Vitest",
    "jest": "Jest",
    "@playwright/test": "Playwright",
    "typescript": "TypeScript",
    "tailwindcss": "Tailwind CSS",
    "zustand": "Zustand",
    "convex": "Convex",
    "leaflet": "Leaflet",
    "recharts": "Recharts",
    "zod": "Zod",
    "express": "Express",
    "puppeteer": "Puppeteer",
    "puppeteer-core": "Puppeteer",
    "eslint": "ESLint",
    "onnxruntime-web": "ONNX Runtime",
    "@modelcontextprotocol/sdk": "MCP",
    "@anthropic-ai/sdk": "Anthropic API",
    "@google/generative-ai": "Gemini API",
    "@google/genai": "Gemini API",
    "@supabase/supabase-js": "Supabase",
    "three": "Three.js",
    # Rust
    "tauri": "Tauri",
    "wgpu": "WebGPU",
    "wasm-bindgen": "WebAssembly",
    "tokio": "Tokio",
}


def technologies(packages: Iterable[str]) -> list[str]:
    """The résumé names for `packages`, sorted, each once. Unknown packages drop out."""
    names = {
        TECHNOLOGY_NAMES[p] for p in (pkg.lower() for pkg in packages) if p in TECHNOLOGY_NAMES
    }
    return sorted(names, key=str.lower)
