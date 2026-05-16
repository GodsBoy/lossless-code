#!/usr/bin/env python3
"""Codex SessionStart hook adapter for lossless-code."""

import json
import os
import sys
from typing import TextIO

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
import inject_context


SUPPORTED_SOURCES = {"startup", "resume", "clear"}
AGENT_SOURCE = "codex-cli"
RESERVED_MARKER_PREFIX = "[lcc."


def _warn(stderr: TextIO, message: str) -> None:
    print(f"[lossless-code] Codex SessionStart: {message}", file=stderr)


def _read_payload(stdin: TextIO, stderr: TextIO) -> dict | None:
    raw = stdin.read()
    if not raw.strip():
        _warn(stderr, "no JSON payload received")
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        _warn(stderr, "invalid JSON payload")
        return None
    if not isinstance(payload, dict):
        _warn(stderr, "payload must be a JSON object")
        return None
    return payload


def _has_unsafe_context_marker(value: str) -> bool:
    return "\n" in value or "\r" in value or RESERVED_MARKER_PREFIX in value


def build_hook_output(payload: dict, stderr: TextIO = sys.stderr) -> dict | None:
    """Return Codex hook JSON for a SessionStart payload, or None for no output."""
    if payload.get("hook_event_name") != "SessionStart":
        return None

    source = payload.get("source")
    if source not in SUPPORTED_SOURCES:
        _warn(stderr, "unsupported source")
        return None

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        _warn(stderr, "missing session_id")
        return None
    session_id = session_id.strip()
    if _has_unsafe_context_marker(session_id):
        _warn(stderr, "unsafe session_id")
        return None

    cwd = payload.get("cwd")
    working_dir = cwd if isinstance(cwd, str) else ""
    if _has_unsafe_context_marker(working_dir):
        _warn(stderr, "unsafe cwd omitted")
        working_dir = ""

    cfg = db.load_config()
    if db.matches_any_pattern(session_id, cfg.get("ignoreSessionPatterns", [])):
        return None
    stateless = db.matches_any_pattern(
        session_id,
        cfg.get("statelessSessionPatterns", []),
    )
    db.ensure_session(
        session_id,
        working_dir,
        stateless=stateless,
        agent_source=AGENT_SOURCE,
    )
    context = inject_context.build_context(
        session_id=session_id,
        working_dir=working_dir,
        agent_source=AGENT_SOURCE,
    )
    if not context:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }


def main(stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout, stderr: TextIO = sys.stderr) -> int:
    payload = _read_payload(stdin, stderr)
    if payload is None:
        return 0
    try:
        output = build_hook_output(payload, stderr=stderr)
    except Exception:
        _warn(stderr, "failed to build context")
        return 0
    if output is not None:
        print(json.dumps(output), file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
