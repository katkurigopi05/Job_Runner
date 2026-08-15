"""Adapter lookup by URL.

Order matters only if two adapters could claim the same URL; today none can.
Adapters register here and nowhere else — adding Lever is one import and one
list entry.
"""

from __future__ import annotations

from packages.ats.base import ATSAdapter, UnsupportedSiteError
from packages.ats.greenhouse import GreenhouseAdapter

ADAPTERS: list[type[ATSAdapter]] = [
    GreenhouseAdapter,
]


def detect_ats(url: str) -> str | None:
    """Name of the ATS behind a URL, or None if nothing claims it."""
    for adapter in ADAPTERS:
        if adapter.matches(url):
            return adapter.name
    return None


def adapter_for(url: str) -> ATSAdapter:
    """Instantiate the adapter for a URL.

    Raises:
        UnsupportedSiteError: nothing handles it — maps to `unsupported_site`.
    """
    for adapter in ADAPTERS:
        if adapter.matches(url):
            return adapter()
    raise UnsupportedSiteError(f"no adapter handles {url}")


def adapter_by_name(name: str) -> ATSAdapter:
    for adapter in ADAPTERS:
        if adapter.name == name:
            return adapter()
    raise UnsupportedSiteError(f"no adapter named {name}")


def supported() -> list[str]:
    return [adapter.name for adapter in ADAPTERS]
