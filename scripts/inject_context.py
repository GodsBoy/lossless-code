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
from summarise import estimate_tokens


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


_CONTROL_CHARS = "".join(chr(c) for c in range(0x20) if c not in (0x09,))


def _sanitize_for_context(value: str, max_len: int = 256) -> str:
    """Strip newlines and control characters, cap length.

    The fingerprint string is injected verbatim into Claude's
    ``additionalContext``. A crafted file path or summary line
    containing newlines could inject extra instructions — treat
    every interpolated value as untrusted.
    """
    if not value:
        return ""
    cleaned = value.replace("\r", " ").replace("\n", " ")
    for ch in _CONTROL_CHARS:
        cleaned = cleaned.replace(ch, " ")
    cleaned = cleaned.strip()
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 1] + "…"
    return cleaned


def format_file_fingerprint(
    file_path: str,
    summaries: list[dict],
    token_budget: int = 200,
) -> str:
    """
    Render a compact fingerprint of prior vault activity on ``file_path``.

    Output shape::

        [lcc] {file_path} — {N} prior summaries, last touched {date},
           polarity: {polarity_counts},
           topics: {topics}.
           Expand: call MCP tool `lcc_expand` with {"file": "{file_path}"}

    Returns an empty string when there are no summaries. The output is
    bounded to ~``token_budget`` tokens (rough 4-chars-per-token heuristic).
    Truncation order when over budget: topics 5 → 3, then drop the
    "last touched" clause. ``file_path`` and the Expand line are never
    truncated — they are the load-bearing content for agents.
    """
    if not summaries:
        return ""

    from collections import Counter
    from datetime import datetime

    file_path = _sanitize_for_context(file_path, max_len=256)
    n = len(summaries)

    # Last touched: newest created_at across the summaries.
    latest = max((s.get("created_at") or 0) for s in summaries)
    last_touched = ""
    if latest:
        last_touched = datetime.fromtimestamp(latest).strftime("%Y-%m-%d")

    # Polarity counts: "edited×2, discussed×1".
    kinds = [s.get("kind") for s in summaries if s.get("kind")]
    if kinds:
        counts = Counter(kinds)
        polarity = ", ".join(f"{k}×{v}" for k, v in counts.most_common())
    else:
        polarity = "unknown"

    # Topics: first few words from each summary's content, dedup-ordered.
    def _topic(content: str) -> str:
        first_line = (content or "").strip().split("\n", 1)[0]
        words = first_line.split()
        return _sanitize_for_context(" ".join(words[:6]), max_len=120)

    seen = set()
    raw_topics = []
    for s in summaries:
        t = _topic(s.get("content", ""))
        if t and t not in seen:
            seen.add(t)
            raw_topics.append(t)

    max_chars = token_budget * 4
    expand_line = (
        f'   Expand: call MCP tool `lcc_expand` with {{"file": "{file_path}"}}'
    )

    def _render(topics_limit: int, include_last_touched: bool) -> str:
        topics = raw_topics[:topics_limit]
        topics_str = "; ".join(topics) if topics else "none"
        parts = [f"[lcc] {file_path} — {n} prior summaries"]
        if include_last_touched and last_touched:
            parts.append(f"last touched {last_touched}")
        parts.append(f"polarity: {polarity}")
        parts.append(f"topics: {topics_str}")
        header = ", ".join(parts) + "."
        return f"{header}\n{expand_line}"

    # Budget ladder: 5 topics + date → 3 topics + date → 3 topics, no date.
    for topics_limit, include_date in ((5, True), (3, True), (3, False)):
        out = _render(topics_limit, include_date)
        if len(out) <= max_chars:
            return out

    # Hard floor: always return the file_path + Expand line intact, even if
    # that means dropping topics entirely.
    return f"[lcc] {file_path} — {n} prior summaries, polarity: {polarity}.\n{expand_line}"


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
    """Get relevant summaries — by FTS search if query given, otherwise top by depth.

    Fetches limit * 3 candidates from the DB to give the budget-aware packer
    a larger pool to select from. The caller decides how many to actually include.
    """
    candidates = limit * 3

    if query and query.strip():
        results = db.search_summaries(query, limit=candidates)
        if results:
            return results

    # Fallback: return highest-depth summaries (most compressed overview)
    return db.get_top_summaries(limit=candidates)


def build_context(
    session_id: str = None,
    working_dir: str = "",
    query: str = "",
    limit: int = 5,
    config_override: dict = None,
) -> str:
    """Build the context block to inject into Claude's session.

    Uses budget-aware per-item packing: handoff and dream patterns are always
    included (reserved budget), then summaries are packed greedily by FTS5
    relevance rank (when query is present) or depth (when no query).
    Individual summaries are never truncated mid-content.
    """
    config = {**db.load_config(), **(config_override or {})}

    ctx_budget = config.get("contextTokenBudget", 8000)

    # --- Phase 1: Build reserved parts (always included) ---
    reserved_parts = []
    reserved_tokens = 0

    # Handoff from previous session
    handoff = get_handoff(session_id)
    handoff_block = ""
    if handoff:
        handoff_block = f"## Previous Session Handoff\n{handoff}"
        reserved_parts.append(handoff_block)
        reserved_tokens += estimate_tokens(handoff_block)

    # Dream patterns (project-specific + global)
    dream_ctx = _load_dream_patterns(working_dir, config)
    dream_block = ""
    if dream_ctx:
        dream_block = f"## Dream Patterns (extracted from history)\n{dream_ctx}"
        reserved_parts.append(dream_block)
        reserved_tokens += estimate_tokens(dream_block)

    # --- Phase 2: Budget-aware summary packing ---
    header = "# Lossless Context (auto-injected)\n"
    section_header = "## Relevant Context (from conversation history)"
    separator = "\n\n"

    # Reserve tokens for header (always present when we have any content)
    header_tokens = estimate_tokens(header)
    # Separator tokens: one separator per reserved part, plus one for section_header
    separator_tokens = estimate_tokens(separator) * (len(reserved_parts) + 1)

    summary_budget = max(0, ctx_budget - reserved_tokens - header_tokens
                         - estimate_tokens(section_header) - separator_tokens)
    candidates = get_relevant_summaries(query, limit=limit)

    selected_summaries = []
    used_tokens = 0

    for s in candidates:
        content = s.get("content", "")
        depth_label = f"depth-{s['depth']}" if "depth" in s else ""
        item_text = f"### [{len(selected_summaries) + 1}] {depth_label}\n{content}"
        item_tokens = estimate_tokens(item_text) + estimate_tokens(separator)

        if used_tokens + item_tokens <= summary_budget:
            selected_summaries.append(item_text)
            used_tokens += item_tokens

    # --- Phase 3: Assemble output ---
    parts = []

    if reserved_parts:
        parts.extend(reserved_parts)

    if selected_summaries:
        parts.append(section_header)
        parts.extend(selected_summaries)

    if not parts:
        return ""

    return header + separator.join(parts)


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
