"""Stage private runtime files outside synced storage without deleting sources."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from packages.core.config import get_settings, settings_env_file
from packages.core.doctor import synced_folder

FORMAT_VERSION = 1
MANIFEST_FILE = "private-path-manifest.json"
RUNTIME_ENV_FILE = "jobrunner.env"
_ROOT_NAMES = ("storage", "vault", "backups")
_ENV_KEYS = {
    "STORAGE_ROOT": "storage",
    "VAULT_ROOT": "vault",
    "BACKUP_ROOT": "backups",
}


class PrivatePathError(RuntimeError):
    """A private-path plan or verified copy is unsafe or incomplete."""


@dataclass(frozen=True, order=True)
class PrivatePathEntry:
    path: str
    kind: str
    sha256: str = ""
    bytes: int = 0


@dataclass(frozen=True)
class PrivatePathPlan:
    destination: Path
    runtime_env: Path
    files: int
    directories: int
    symlinks: int
    bytes: int


@dataclass(frozen=True)
class PrivatePathResult:
    destination: Path
    runtime_env: Path
    files: int
    bytes: int
    already_complete: bool


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _manifest_digest(entries: Iterable[PrivatePathEntry]) -> str:
    encoded = json.dumps(
        [asdict(entry) for entry in sorted(entries)],
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _entries(root: Path, logical_root: str) -> list[PrivatePathEntry]:
    if not root.exists():
        return []
    if not root.is_dir():
        raise PrivatePathError(f"configured {logical_root} root is not a directory")
    entries: list[PrivatePathEntry] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        logical = f"{logical_root}/{relative}"
        if path.is_symlink():
            target = os.readlink(path)
            entries.append(
                PrivatePathEntry(logical, "symlink", _sha256_text(target), len(target.encode()))
            )
        elif path.is_dir():
            entries.append(PrivatePathEntry(logical, "directory"))
        elif path.is_file():
            entries.append(
                PrivatePathEntry(logical, "file", _sha256_file(path), path.stat().st_size)
            )
        else:
            raise PrivatePathError(f"configured {logical_root} contains an unsupported file")
    return entries


def _validate_symlinks(root: Path, label: str) -> None:
    if not root.exists():
        return
    if root.is_symlink():
        raise PrivatePathError(f"configured {label} root must not itself be a symlink")
    resolved_root = root.resolve()
    for path in root.rglob("*"):
        if not path.is_symlink():
            continue
        link = Path(os.readlink(path))
        if link.is_absolute():
            raise PrivatePathError(f"configured {label} contains an absolute symlink")
        try:
            target = path.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise PrivatePathError(f"configured {label} contains an invalid symlink") from exc
        if not _inside(target, resolved_root):
            raise PrivatePathError(f"configured {label} contains a symlink outside its root")


def _source_roots() -> dict[str, Path]:
    settings = get_settings()
    return {
        "storage": Path(settings.storage_root).expanduser().resolve(),
        "vault": Path(settings.vault_root).expanduser().resolve(),
        "backups": Path(settings.backup_root).expanduser().resolve(),
    }


def _source_entries(roots: dict[str, Path], env_file: Path) -> list[PrivatePathEntry]:
    entries = [entry for name, root in roots.items() for entry in _entries(root, name)]
    entries.append(
        PrivatePathEntry(
            RUNTIME_ENV_FILE,
            "file",
            _sha256_file(env_file),
            env_file.stat().st_size,
        )
    )
    return sorted(entries)


def _artifact_entries(destination: Path) -> list[PrivatePathEntry]:
    return sorted(entry for name in _ROOT_NAMES for entry in _entries(destination / name, name))


def _validate_plan(
    destination: Path,
    roots: dict[str, Path],
    env_file: Path,
    *,
    managed: Callable[[Path], bool] | None,
    home: Path | None,
) -> None:
    if not env_file.is_file():
        raise PrivatePathError("the selected settings file does not exist")
    if destination.exists() and not (destination / MANIFEST_FILE).is_file():
        raise PrivatePathError("destination already exists without a verified migration manifest")
    if synced_folder(destination, managed=managed, home=home) is not None:
        raise PrivatePathError("destination is inside a folder managed by a sync client")
    sources = [*roots.values(), env_file]
    for source in sources:
        if _inside(destination, source) or _inside(source, destination):
            raise PrivatePathError("destination must not overlap a source path")
    existing_roots = [root for root in roots.values() if root.exists()]
    for index, left in enumerate(existing_roots):
        for right in existing_roots[index + 1 :]:
            if _inside(left, right) or _inside(right, left):
                raise PrivatePathError("storage, vault and backup roots must not overlap")
    for name, root in roots.items():
        _validate_symlinks(root, name)


def plan_private_paths(
    destination: Path,
    *,
    managed: Callable[[Path], bool] | None = None,
    home: Path | None = None,
) -> PrivatePathPlan:
    """Validate and inventory a copy without writing anything."""
    target = destination.expanduser().resolve()
    env_file = settings_env_file()
    roots = _source_roots()
    _validate_plan(target, roots, env_file, managed=managed, home=home)
    entries = _source_entries(roots, env_file)
    return PrivatePathPlan(
        destination=target,
        runtime_env=target / RUNTIME_ENV_FILE,
        files=sum(entry.kind == "file" for entry in entries),
        directories=sum(entry.kind == "directory" for entry in entries),
        symlinks=sum(entry.kind == "symlink" for entry in entries),
        bytes=sum(entry.bytes for entry in entries),
    )


def _env_value(path: Path) -> str:
    value = str(path)
    if re.fullmatch(r"[A-Za-z0-9_./:@+\-]+", value):
        return value
    return json.dumps(value)


def _rewrite_runtime_env(path: Path, destination: Path) -> None:
    replacements = {key: _env_value(destination / folder) for key, folder in _ENV_KEYS.items()}
    lines = path.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    rewritten: list[str] = []
    pattern = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
    for line in lines:
        match = pattern.match(line)
        key = match.group(1) if match else ""
        if key in replacements:
            rewritten.append(f"{key}={replacements[key]}")
            seen.add(key)
        else:
            rewritten.append(line)
    for key in _ENV_KEYS:
        if key not in seen:
            rewritten.append(f"{key}={replacements[key]}")
    path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")


def _restrict_permissions(root: Path) -> None:
    root.chmod(0o700)
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        path.chmod(0o700 if path.is_dir() else 0o600)


def _verify_permissions(root: Path) -> None:
    for path in (root, *root.rglob("*")):
        if path.is_symlink():
            continue
        expected = 0o700 if path.is_dir() else 0o600
        if path.stat().st_mode & 0o777 != expected:
            raise PrivatePathError("private-path permissions no longer match the migration policy")


def _read_manifest(destination: Path) -> dict[str, Any]:
    path = destination / MANIFEST_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PrivatePathError("destination migration manifest is missing or invalid") from exc
    if not isinstance(raw, dict) or raw.get("format_version") != FORMAT_VERSION:
        raise PrivatePathError("destination migration manifest has an unsupported format")
    return raw


def verify_private_paths(destination: Path) -> PrivatePathResult:
    """Verify copied artifacts and the rewritten runtime settings file."""
    target = destination.expanduser().resolve()
    raw = _read_manifest(target)
    try:
        expected = [PrivatePathEntry(**entry) for entry in raw["artifacts"]]
        runtime_env_name = str(raw["runtime_env"]["path"])
        if runtime_env_name != RUNTIME_ENV_FILE:
            raise PrivatePathError("destination migration manifest names an unsafe settings path")
        runtime_env = target / runtime_env_name
        runtime_sha256 = str(raw["runtime_env"]["sha256"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PrivatePathError("destination migration manifest is incomplete") from exc
    actual = _artifact_entries(target)
    if actual != sorted(expected) or _manifest_digest(actual) != raw.get("artifacts_sha256"):
        raise PrivatePathError("private files no longer match the migration manifest")
    if not runtime_env.is_file() or _sha256_file(runtime_env) != runtime_sha256:
        raise PrivatePathError("runtime settings no longer match the migration manifest")
    _verify_permissions(target)
    return PrivatePathResult(
        destination=target,
        runtime_env=runtime_env,
        files=sum(entry.kind == "file" for entry in actual) + 1,
        bytes=sum(entry.bytes for entry in actual) + runtime_env.stat().st_size,
        already_complete=False,
    )


def _copy_sources(stage: Path, roots: dict[str, Path], env_file: Path) -> None:
    for name, source in roots.items():
        target = stage / name
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True)
        else:
            target.mkdir()
    shutil.copy2(env_file, stage / RUNTIME_ENV_FILE)


def migrate_private_paths(
    destination: Path,
    *,
    managed: Callable[[Path], bool] | None = None,
    home: Path | None = None,
) -> PrivatePathResult:
    """Copy, verify, restrict and atomically publish private runtime files."""
    target = destination.expanduser().resolve()
    env_file = settings_env_file()
    roots = _source_roots()
    _validate_plan(target, roots, env_file, managed=managed, home=home)
    source_entries = _source_entries(roots, env_file)
    source_digest = _manifest_digest(source_entries)
    if target.exists():
        result = verify_private_paths(target)
        raw = _read_manifest(target)
        if raw.get("verified_source_sha256") != source_digest:
            raise PrivatePathError("source files changed after this destination was migrated")
        return PrivatePathResult(
            destination=result.destination,
            runtime_env=result.runtime_env,
            files=result.files,
            bytes=result.bytes,
            already_complete=True,
        )

    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        _copy_sources(stage, roots, env_file)
        copied_entries = _artifact_entries(stage)
        copied_entries.append(
            PrivatePathEntry(
                RUNTIME_ENV_FILE,
                "file",
                _sha256_file(stage / RUNTIME_ENV_FILE),
                (stage / RUNTIME_ENV_FILE).stat().st_size,
            )
        )
        if sorted(copied_entries) != source_entries:
            raise PrivatePathError("copied private files did not match the source manifest")
        if _source_entries(roots, env_file) != source_entries:
            raise PrivatePathError("source files changed while the private copy was being made")

        runtime_env = stage / RUNTIME_ENV_FILE
        _rewrite_runtime_env(runtime_env, target)
        artifacts = sorted(entry for entry in copied_entries if entry.path != RUNTIME_ENV_FILE)
        manifest = {
            "format_version": FORMAT_VERSION,
            "verified_source_sha256": source_digest,
            "artifacts_sha256": _manifest_digest(artifacts),
            "artifacts": [asdict(entry) for entry in artifacts],
            "runtime_env": {
                "path": RUNTIME_ENV_FILE,
                "source_sha256": next(
                    entry.sha256 for entry in source_entries if entry.path == RUNTIME_ENV_FILE
                ),
                "sha256": _sha256_file(runtime_env),
            },
            "source_retained": True,
        }
        (stage / MANIFEST_FILE).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _restrict_permissions(stage)
        stage.rename(target)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return verify_private_paths(target)


__all__ = [
    "MANIFEST_FILE",
    "RUNTIME_ENV_FILE",
    "PrivatePathError",
    "PrivatePathPlan",
    "PrivatePathResult",
    "migrate_private_paths",
    "plan_private_paths",
    "verify_private_paths",
]
