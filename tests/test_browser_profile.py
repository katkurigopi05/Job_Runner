"""Browser profile isolation.

Chromium does not refuse a second launch against a profile directory already
in use — it silently proceeds, and two processes then share one cookie store.
The lock turns that into a loud failure.
"""

from __future__ import annotations

import pytest

from apps.worker.browser import ProfileInUseError, _profile_lock, profile_dir


def test_profile_dir_is_per_worker(tmp_path, monkeypatch) -> None:
    from packages.core.config import get_settings

    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    try:
        a = profile_dir("greenhouse", worker_id="worker-a")
        b = profile_dir("greenhouse", worker_id="worker-b")
        assert a != b
    finally:
        get_settings.cache_clear()


def test_profile_dir_separates_ats(tmp_path, monkeypatch) -> None:
    from packages.core.config import get_settings

    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    try:
        assert profile_dir("greenhouse", worker_id="w") != profile_dir("lever", worker_id="w")
    finally:
        get_settings.cache_clear()


async def test_second_holder_is_refused(tmp_path) -> None:
    async with _profile_lock(tmp_path):
        with pytest.raises(ProfileInUseError, match="already in use"):
            async with _profile_lock(tmp_path):
                pass


async def test_lock_is_released_after_use(tmp_path) -> None:
    async with _profile_lock(tmp_path):
        pass
    # Re-acquiring must succeed, or a worker could never run twice.
    async with _profile_lock(tmp_path):
        pass
