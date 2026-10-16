"""Small durable JSONL journals shared by local Jobrunner processes."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

_PRUNED_ON: dict[tuple[Path, int], date] = {}


def _lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


@contextmanager
def locked(path: Path, *, exclusive: bool) -> Iterator[None]:
    """Hold a process-safe lock for one journal operation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock_path(path).open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def read_payloads_unlocked(path: Path) -> list[dict[str, Any]]:
    """Read complete JSON objects. The caller owns the journal lock."""
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def append_unlocked(path: Path, payload: dict[str, Any]) -> None:
    """Append and flush one object before the caller releases its lock."""
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def prune_unlocked(
    path: Path,
    *,
    retention_days: int,
    now: datetime | None = None,
    force: bool = False,
) -> int:
    """Atomically remove expired valid records, preserving unreadable lines."""
    current = now or datetime.now(UTC)
    today = current.date()
    key = (path.resolve(), retention_days)
    if not force and _PRUNED_ON.get(key) == today:
        return 0
    if not path.is_file():
        _PRUNED_ON[key] = today
        return 0

    cutoff = current - timedelta(days=retention_days)
    lines = path.read_text(encoding="utf-8").splitlines()
    kept: list[str] = []
    removed = 0
    for line in lines:
        if not line:
            continue
        try:
            value = json.loads(line)
            at = datetime.fromisoformat(value["at"])
            if at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            kept.append(line)
            continue
        if at >= cutoff:
            kept.append(line)
        else:
            removed += 1

    if removed == 0:
        _PRUNED_ON[key] = today
        return 0

    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary = handle.name
            if kept:
                handle.write("\n".join(kept) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        if temporary is not None:
            with suppress(OSError):
                Path(temporary).unlink()
        raise
    _PRUNED_ON[key] = today
    return removed
