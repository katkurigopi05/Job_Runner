"""`make doctor` — check this machine before a run.

Prints one line per check and exits non-zero when something required is
broken, so it is usable as a precondition in a script and not only by eye.
"""

from __future__ import annotations

import asyncio
import sys

from packages.core.doctor import Health, Report, run

_MARK = {Health.OK: "ok  ", Health.FAIL: "FAIL", Health.SKIPPED: "skip"}


def render(report: Report) -> str:
    lines = []
    for check in report.checks:
        mark = _MARK[check.health]
        suffix = "" if check.required else "  (optional)"
        lines.append(f"  [{mark}] {check.name:<12} {check.detail}{suffix}")
        if check.fix and not check.ok:
            lines.append(f"         fix: {check.fix}")
    lines.append("")
    lines.append(f"  {report.summary()}")
    return "\n".join(lines)


def main() -> int:
    report = asyncio.run(run())
    print(render(report))
    return 0 if report.healthy else 1


if __name__ == "__main__":
    sys.exit(main())
