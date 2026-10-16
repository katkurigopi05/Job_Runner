"""Plan or stage private runtime files outside provider-managed folders."""

from __future__ import annotations

import argparse
import shlex
from pathlib import Path

from packages.core.config import SettingsEnvFileError
from packages.core.private_paths import PrivatePathError, migrate_private_paths, plan_private_paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="copy and verify files; the default is a dry run",
    )
    args = parser.parse_args(argv)
    try:
        if not args.apply:
            plan = plan_private_paths(args.destination)
            print(f"destination: {plan.destination}")
            print(
                f"inventory: {plan.files} files, {plan.directories} directories, "
                f"{plan.symlinks} symlinks, {plan.bytes:,} bytes"
            )
            print("DRY RUN: nothing copied; stop services before rerunning with apply=1")
            return 0
        result = migrate_private_paths(args.destination)
    except (PrivatePathError, SettingsEnvFileError) as exc:
        parser.error(str(exc))
    except OSError:
        parser.error("private-path operation failed; no source files were deleted")
    state = "already verified" if result.already_complete else "copied and verified"
    print(f"destination: {result.destination}")
    print(f"verified: {result.files} files, {result.bytes:,} bytes")
    print(f"private runtime files {state}; source files were retained")
    print(f"export JOBRUNNER_ENV_FILE={shlex.quote(str(result.runtime_env))}")
    print("restart services with that export, then run `make doctor`")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
