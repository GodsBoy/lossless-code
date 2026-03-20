#!/usr/bin/env python3
"""
Context injection for lossless-code.

Called by hooks to surface relevant DAG summaries and handoff text
for the current session/query.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db


def get_handoff(session_id: str = None) -> str:
    """Get handoff text from the most recent session (or a specific one)."""
    if session_id:
        session = db.get_session(session_id)
        if session and session.get("handoff_text"):
            return session["handoff_text"]

    # Fall back to most recent session with a handoff
    sessions = db.list_sessions(limit=10)
    for s in sessions:
        if s.get("handoff_text"):
            return s["handoff_text"]
    return ""


def get_relevant_summaries(query: str = "", limit: int = 5) -> list[dict]:
    """Get relevant summaries — by search if query given, otherwise top by depth."""
    if query:
        results = db.search_summaries(query, limit=limit)
        if results:
            return results

    # Fallback: return highest-depth summaries (most compressed overview)
    return db.get_top_summaries(limit=limit)


def build_context(
    session_id: str = None,
    working_dir: str = "",
    query: str = "",
    limit: int = 5,
) -> str:
    """Build the context block to inject into Claude's session."""
    parts = []

    # Handoff from previous session
    handoff = get_handoff(session_id)
    if handoff:
        parts.append(f"## Previous Session Handoff\n{handoff}")

    # Relevant summaries
    summaries = get_relevant_summaries(query, limit=limit)
    if summaries:
        parts.append("## Relevant Context (from conversation history)")
        for i, s in enumerate(summaries, 1):
            depth_label = f"depth-{s['depth']}" if 'depth' in s else ""
            parts.append(f"### [{i}] {depth_label}\n{s['content']}")

    if not parts:
        return ""

    header = "# Lossless Context (auto-injected)\n"
    return header + "\n\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description="Inject context for Claude session")
    parser.add_argument("--session", help="Current session ID")
    parser.add_argument("--dir", help="Working directory")
    parser.add_argument("--query", default="", help="Query to find relevant context")
    parser.add_argument("--limit", type=int, default=5, help="Max summaries")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    context = build_context(
        session_id=args.session,
        working_dir=args.dir,
        query=args.query,
        limit=args.limit,
    )

    if args.json:
        print(json.dumps({"context": context}))
    else:
        print(context)


if __name__ == "__main__":
    main()
