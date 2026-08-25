#!/usr/bin/env python3
"""Migration safety check — docs/CI_CD.md §3a.

Scans Alembic migration files that are new or changed on this branch (relative
to the PR's base branch) for destructive operations:

  - `drop_column`
  - `drop_table`
  - a type change that could narrow precision (`alter_column(..., type_=...)`
    — flagged unconditionally, since telling a widening change from a
    narrowing one requires the old column definition, which isn't reliably
    recoverable from the migration file alone; conservatively flagging every
    type change forces the same conscious decision either way)

Any match fails the build *unless* the migration file contains an explicit
`# BREAKING: <reason>` comment — this is a forcing function for a conscious
decision, not an accidental one, whenever a migration could lose data.

Usage:
    python scripts/check_migration_safety.py [--base-ref REF]

`--base-ref` defaults to the `MIGRATION_SAFETY_BASE_REF` env var, then
`origin/main`. Run from the `backend/` directory (migration files are
resolved relative to `alembic/versions/`).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

MIGRATIONS_DIR = Path("alembic/versions")

DESTRUCTIVE_PATTERNS = {
    "drop_column": re.compile(r"\bdrop_column\s*\("),
    "drop_table": re.compile(r"\bdrop_table\s*\("),
    "type change (alter_column ... type_=...)": re.compile(r"\balter_column\s*\([^)]*\btype_\s*="),
}

BREAKING_MARKER = re.compile(r"#\s*BREAKING\s*:\s*\S")


def run_git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{result.stderr}")
    return result.stdout


def changed_migration_files(base_ref: str) -> list[Path]:
    """Migration files added or modified relative to `base_ref`."""
    diff_output = run_git("diff", "--name-status", f"{base_ref}...HEAD", "--", str(MIGRATIONS_DIR))
    files = []
    for line in diff_output.splitlines():
        if not line.strip():
            continue
        status, _, path = line.partition("\t")
        # Only added/modified files matter here — a deleted migration file
        # doesn't newly introduce a destructive change to review.
        if status.startswith(("A", "M")) and path.endswith(".py"):
            files.append(Path(path))
    return files


def find_destructive_operations(content: str) -> list[str]:
    return [name for name, pattern in DESTRUCTIVE_PATTERNS.items() if pattern.search(content)]


def check_file(path: Path) -> str | None:
    """Returns an error message if `path` has an unmarked destructive change."""
    if not path.exists():
        print(f"  {path}: deleted since base ref, skipping")
        return None
    content = path.read_text()
    destructive = find_destructive_operations(content)
    if not destructive:
        print(f"  {path}: no destructive changes detected")
        return None
    if BREAKING_MARKER.search(content):
        print(f"  {path}: destructive change(s) {destructive}, marked # BREAKING — OK")
        return None
    return (
        f"{path}: contains destructive change(s) {destructive} "
        f"with no '# BREAKING: <reason>' comment.\n"
        f"    Add a comment like '# BREAKING: drops legacy column, backfilled in prior release' "
        f"to this migration file to acknowledge the data-loss risk."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-ref",
        default=None,
        help="Git ref to diff against (default: $MIGRATION_SAFETY_BASE_REF or origin/main)",
    )
    args = parser.parse_args()

    base_ref = args.base_ref or _default_base_ref()

    try:
        files = changed_migration_files(base_ref)
    except RuntimeError as exc:
        print(f"Could not diff against '{base_ref}': {exc}", file=sys.stderr)
        return 1

    if not files:
        print(f"No changed migration files relative to {base_ref}.")
        return 0

    print(f"Checking {len(files)} changed migration file(s) relative to {base_ref}:")
    errors = [error for path in files if (error := check_file(path)) is not None]

    if errors:
        print("\nMigration safety check FAILED:\n", file=sys.stderr)
        for error in errors:
            print(f"  - {error}\n", file=sys.stderr)
        return 1

    print("\nMigration safety check passed.")
    return 0


def _default_base_ref() -> str:
    return os.environ.get("MIGRATION_SAFETY_BASE_REF", "origin/main")


if __name__ == "__main__":
    sys.exit(main())
