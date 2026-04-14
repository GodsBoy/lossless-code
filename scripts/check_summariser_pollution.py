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

import json
import os
import sys
from pathlib import Path
from typing import Any

LOSSLESS_CLI_CWD_BUCKET = "-root--lossless-code--cli-cwd"
SUMMARISER_PROMPT_PREFIX = "Summarise the following conversation turns concisely"
SCAN_LINES = 50


def projects_dir() -> Path:
    override = os.environ.get("LOSSLESS_CHECK_PROJECTS_DIR")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "projects"


def _extract_text(content: Any) -> str:
    """Pull plain text out of a message content field that may be a string
    or a list of content blocks (Anthropic message format)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                txt = block.get("text") or block.get("content") or ""
                if isinstance(txt, str):
                    parts.append(txt)
        return "".join(parts)
    return ""


def file_is_polluting(path: Path) -> bool:
    """A file is polluting iff its first user message's content text starts
    with the summariser prompt. Substring-anywhere matches are too noisy
    because legitimate sessions can mention the prompt in passing."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= SCAN_LINES:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("type") != "user":
                    continue
                msg = rec.get("message") or {}
                if not isinstance(msg, dict):
                    return False
                text = _extract_text(msg.get("content", "")).lstrip()
                return text.startswith(SUMMARISER_PROMPT_PREFIX)
    except OSError as exc:
        print(f"warning: could not read {path}: {exc}", file=sys.stderr)
        return False
    return False


def find_polluting(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    hits: list[Path] = []
    for bucket in sorted(root.iterdir()):
        if not bucket.is_dir() or bucket.name == LOSSLESS_CLI_CWD_BUCKET:
            continue
        for entry in bucket.rglob("*.jsonl"):
            if entry.is_file() and file_is_polluting(entry):
                hits.append(entry)
    return hits


def main() -> int:
    hits = find_polluting(projects_dir())
    if not hits:
        return 0
    print(f"Found {len(hits)} polluting jsonl file(s) outside {LOSSLESS_CLI_CWD_BUCKET}:")
    for path in hits:
        print(f"  {path}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
