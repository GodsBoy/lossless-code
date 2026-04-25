#!/usr/bin/env python3
"""Hook helper: store a PostToolUse tool call in the vault with file_path tag.

Captures Read/Edit/Write/MultiEdit/NotebookEdit tool calls as ``role='tool'``
messages with ``file_path`` populated so the fingerprint file-context system
can surface prior activity on the same file via a PreToolUse hook.

Gated on ``fileContextEnabled`` config flag (default off).
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

# Tool names whose inputs carry a file_path we want to track.
FILE_TOOLS = {"Read", "Edit", "Write", "MultiEdit", "NotebookEdit"}


def _extract_file_path(tool_name: str, tool_input: dict) -> str | None:
    if tool_name not in FILE_TOOLS:
        return None
    for key in ("file_path", "notebook_path", "path"):
        val = tool_input.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def _normalize_path(path: str, cwd: str) -> str:
    """Return ``path`` relative to ``cwd`` when it lies under ``cwd``; absolute otherwise."""
    if not path:
        return path
    try:
        abs_path = os.path.abspath(path)
    except (OSError, ValueError):
        return path
    if cwd:
        try:
            abs_cwd = os.path.abspath(cwd)
            if abs_path == abs_cwd or abs_path.startswith(abs_cwd + os.sep):
                return os.path.relpath(abs_path, abs_cwd)
        except (OSError, ValueError):
            pass
    return abs_path


def _summarize(tool_name: str, tool_input: dict, tool_response: dict) -> str:
    """Short one-line content for the stored tool message."""
    file_hint = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    status = "ok"
    if isinstance(tool_response, dict):
        if tool_response.get("error") or tool_response.get("is_error"):
            status = "error"
    return f"{tool_name}: {file_hint} ({status})"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    parser.add_argument("--dir", default="")
    parser.add_argument("--payload", default="")
    args = parser.parse_args()

    cfg = db.load_config()
    if not cfg.get("fileContextEnabled", False):
        return
    if db.matches_any_pattern(args.session, cfg.get("ignoreSessionPatterns", [])):
        return

    # Payload is the raw PostToolUse JSON, either from --payload or stdin.
    raw = args.payload or sys.stdin.read()
    if not raw.strip():
        return
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input") or {}
    tool_response = data.get("tool_response") or {}
    raw_file_path = _extract_file_path(tool_name, tool_input)
    if not raw_file_path:
        return

    file_path = _normalize_path(raw_file_path, args.dir)

    stateless = db.matches_any_pattern(
        args.session, cfg.get("statelessSessionPatterns", [])
    )
    db.ensure_session(args.session, args.dir, stateless=stateless)

    # v1.2 span fields. PostToolUse payloads expose tool_use_id (Claude Code's
    # internal call id) under that key; defensively check a couple of name
    # variants in case the payload shape shifts. parent_message_id stays NULL
    # for v1.2 (would require a DB lookup we explicitly do not do at write
    # time, per docs/plans/2026-04-25-001-feat-v12-compaction-aware-bundle-plan.md
    # U3 approach notes).
    tool_call_id = (
        data.get("tool_use_id") or data.get("tool_call_id") or data.get("id") or None
    )
    if tool_call_id is not None:
        tool_call_id = str(tool_call_id)
    attributes = {"tool_name": tool_name}
    if isinstance(tool_response, dict):
        # Surface the error flag in attributes so the bundle assembler / lcc_grep
        # can spot failure spans without parsing content.
        if tool_response.get("error") or tool_response.get("is_error"):
            attributes["error"] = True

    db.store_message(
        session_id=args.session,
        role="tool",
        content=_summarize(tool_name, tool_input, tool_response),
        tool_name=tool_name,
        working_dir=args.dir,
        file_path=file_path,
        span_kind="tool_call",
        tool_call_id=tool_call_id,
        attributes=attributes,
    )


if __name__ == "__main__":
    main()
