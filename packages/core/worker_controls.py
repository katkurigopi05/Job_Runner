"""Runtime crawler pause/resume state and worker task-kind / concurrency controls.

Persisted locally under `storage/worker-controls.json` (or the path in
`JOBRUNNER_WORKER_CONTROLS_FILE`) so the dashboard and background worker
processes share the owner's runtime choices without external state (CLAUDE.md §1).
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

ALL_WORKER_KINDS: tuple[str, ...] = (
    "rescore",
    "apply",
    "crawl",
    "crawl_company",
    "discover_company",
    "deep_dive_company",
    "discover",
    "inbox",
)

CRAWLER_TASK_KINDS: frozenset[str] = frozenset(
    {
        "crawl",
        "crawl_company",
        "discover_company",
        "deep_dive_company",
        "discover",
    }
)

DEFAULT_CONTROLS_PATH = Path("storage/worker-controls.json")
_LOCK = threading.Lock()


@dataclass(frozen=True)
class WorkerControlsState:
    """Owner-selected crawler pause state, enabled worker kinds, and concurrency cap."""

    crawler_paused: bool = False
    enabled_kinds: list[str] = field(default_factory=lambda: list(ALL_WORKER_KINDS))
    max_workers: int = 1

    def active_kinds(self, available: list[str] | tuple[str, ...]) -> list[str]:
        """Filter `available` handler kinds by `enabled_kinds` and `crawler_paused`."""
        allowed = set(self.enabled_kinds)
        out: list[str] = []
        for kind in available:
            if kind not in allowed:
                continue
            if self.crawler_paused and kind in CRAWLER_TASK_KINDS:
                continue
            out.append(kind)
        return out


def _controls_path() -> Path:
    override = os.environ.get("JOBRUNNER_WORKER_CONTROLS_FILE", "").strip()
    if override:
        return Path(override)
    return DEFAULT_CONTROLS_PATH


def _sanitize_kinds(kinds: list[str] | tuple[str, ...] | None) -> list[str]:
    if kinds is None:
        return list(ALL_WORKER_KINDS)
    valid = set(ALL_WORKER_KINDS)
    cleaned: list[str] = []
    for item in kinds:
        k = str(item).strip()
        if k in valid and k not in cleaned:
            cleaned.append(k)
    return cleaned


def _sanitize_max_workers(value: int | None) -> int:
    if value is None:
        return 1
    return max(1, min(8, int(value)))


def load_worker_controls() -> WorkerControlsState:
    """Read current worker controls from disk, falling back to safe defaults."""
    path = _controls_path()
    with _LOCK:
        if not path.exists():
            return WorkerControlsState()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return WorkerControlsState()
        if not isinstance(raw, dict):
            return WorkerControlsState()
        paused = bool(raw.get("crawler_paused", False))
        raw_kinds = raw.get("enabled_kinds")
        kinds = (
            _sanitize_kinds(raw_kinds) if isinstance(raw_kinds, list) else list(ALL_WORKER_KINDS)
        )
        raw_max = raw.get("max_workers", 1)
        try:
            max_w = _sanitize_max_workers(int(raw_max))
        except (ValueError, TypeError):
            max_w = 1
        return WorkerControlsState(
            crawler_paused=paused,
            enabled_kinds=kinds,
            max_workers=max_w,
        )


def save_worker_controls(
    *,
    crawler_paused: bool | None = None,
    enabled_kinds: list[str] | None = None,
    max_workers: int | None = None,
) -> WorkerControlsState:
    """Atomically update and persist worker controls on disk."""
    current = load_worker_controls()
    updated = WorkerControlsState(
        crawler_paused=current.crawler_paused if crawler_paused is None else bool(crawler_paused),
        enabled_kinds=(
            current.enabled_kinds if enabled_kinds is None else _sanitize_kinds(enabled_kinds)
        ),
        max_workers=(
            current.max_workers if max_workers is None else _sanitize_max_workers(max_workers)
        ),
    )
    path = _controls_path()
    payload = {
        "crawler_paused": updated.crawler_paused,
        "enabled_kinds": updated.enabled_kinds,
        "max_workers": updated.max_workers,
    }
    with _LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass
    return updated
