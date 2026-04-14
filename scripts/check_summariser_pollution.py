#!/usr/bin/env python3
"""Detect summariser CWD pollution in Claude Code project session buckets.

Walks ``~/.claude/projects/`` (override with ``LOSSLESS_CHECK_PROJECTS_DIR``),
skipping the legitimate ``-root--lossless-code--cli-cwd`` bucket. Any ``.jsonl``
file containing the lossless-code summariser prompt outside that bucket is a
regression of the cwd-pinning fix and causes a non-zero exit.

Exit codes:
    0  no pollution found (or projects dir missing)
    1  one or more polluting files found (printed to stdout)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

LEGIT_BUCKET = "-root--lossless-code--cli-cwd"
NEEDLE = (
    b"Summarise the following conversation turns concisely, "
    b"preserving all key decisions, facts, file paths, commands"
)
SCAN_BYTES = 8192


def projects_dir() -> Path:
    override = os.environ.get("LOSSLESS_CHECK_PROJECTS_DIR")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "projects"


def file_is_polluting(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            head = fh.read(SCAN_BYTES)
    except OSError:
        return False
    return NEEDLE in head


def find_polluting(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    hits: list[Path] = []
    for bucket in sorted(root.iterdir()):
        if not bucket.is_dir() or bucket.name == LEGIT_BUCKET:
            continue
        for entry in bucket.rglob("*.jsonl"):
            if entry.is_file() and file_is_polluting(entry):
                hits.append(entry)
    return hits


def main() -> int:
    hits = find_polluting(projects_dir())
    if not hits:
        return 0
    print(f"Found {len(hits)} polluting jsonl file(s) outside {LEGIT_BUCKET}:")
    for path in hits:
        print(f"  {path}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
