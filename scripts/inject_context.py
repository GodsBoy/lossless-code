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


def _load_dream_patterns(working_dir: str = "", config: dict = None) -> str:
    """Load dream patterns for injection into session context.

    Reads per-project patterns (if working_dir matches) and global patterns.
    Combines within the configured token budget.
    """
    if config is None:
        config = db.load_config()

    token_budget = config.get("dreamTokenBudget", 2000)
    dream_dir = db.VAULT_DIR / "dream"
    parts = []

    # Per-project patterns
    if working_dir:
        phash = db.project_hash(working_dir)
        project_path = dream_dir / "projects" / phash / "patterns.md"
        if project_path.exists():
            try:
                content = project_path.read_text().strip()
                if content:
                    parts.append(content)
            except OSError:
                pass

    # Global patterns
    global_path = dream_dir / "global" / "patterns.md"
    if global_path.exists():
        try:
            content = global_path.read_text().strip()
            if content:
                parts.append(content)
        except OSError:
            pass

    if not parts:
        return ""

    combined = "\n\n".join(parts)

    # Truncate to token budget (rough estimate: 4 chars per token)
    max_chars = token_budget * 4
    if len(combined) > max_chars:
        combined = combined[:max_chars] + "\n... [truncated to token budget]"

    return combined


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

    # Dream patterns (project-specific + global)
    dream_ctx = _load_dream_patterns(working_dir)
    if dream_ctx:
        parts.append(f"## Dream Patterns (extracted from history)\n{dream_ctx}")

    if not parts:
        return ""

    header = "# Lossless Context (auto-injected)\n"
    combined = header + "\n\n".join(parts)

    # Respect contextTokenBudget to avoid blowing context on session start
    config = db.load_config()
    ctx_budget = config.get("contextTokenBudget", 8000)
    max_chars = ctx_budget * 4  # ~4 chars per token
    if len(combined) > max_chars:
        combined = combined[:max_chars] + "\n\n... [truncated to contextTokenBudget]"

    return combined


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
