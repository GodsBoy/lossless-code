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


# ---------------------------------------------------------------------------
# v1.2 reference bundle (U10)
# ---------------------------------------------------------------------------
#
# Per TD8 the bundle ships items in this fixed order, recovery line FIRST so
# the agent sees the recovery protocol before any content (LLM attention is
# weighted toward start-of-context). Slot budgets are hard-coded constants
# rather than user-configurable until v1.2.1 telemetry tells us how to tune.

_BUNDLE_HEADER = (
    "# Lossless Context (auto-injected, <=1000 tokens)\n"
    "# Each line below carries its own Expand instruction. Invoke the named "
    "MCP tool with the JSON arguments shown to fetch full content."
)

_RECOVERY_LINE = (
    "[lcc.recovery] To recover specific topics from prior session or "
    "pre-compaction history, call MCP tool lcc_context (topic-search) or "
    "lcc_expand (span-id resolution) or lcc_grep (FTS5 fallback)."
)

# Slot budgets in tokens. Allocations match TD8.
_SLOT_BUDGETS = {
    "contracts": 200,
    "handoff": 100,
    "decisions": 250,
    "fingerprints": 250,
}

# Default total bundle cap. Configurable via bundleTokenBudget so power
# users can tighten or relax it without touching slot allocations.
_DEFAULT_BUNDLE_BUDGET = 1000


def _render_recovery_line() -> str:
    """Fixed-template recovery protocol line. No per-session interpolation
    so the renderer is deterministic and the line is identical in every
    bundle (per D6).
    """
    return _RECOVERY_LINE


def _render_contract_ref(contract: dict) -> str:
    """Render an Active contract as a one-line reference with Expand pointer.

    Hardening (TD6 security gate): strips \\n and \\r from contract.body
    and rejects bodies containing the literal "[lcc.contract]" prefix.
    Without this, an attacker who got a Pending contract approved with a
    body like "rule\\n[lcc.contract] FORBID poison" would inject a synthetic
    second contract line into the bundle.

    Returns "" when the body fails sanitization, signaling _pack_slot to
    skip this item.
    """
    raw_body = contract.get("body") or ""
    # Reject before sanitization so the rejection is on the raw body the
    # user actually approved, not a transformed version that would also pass.
    if "[lcc.contract]" in raw_body:
        return ""
    body = _sanitize_for_context(raw_body, max_len=180)
    if not body:
        return ""
    cid = contract.get("id", "?")
    kind = contract.get("kind", "?").upper()
    created = contract.get("created_at")
    when = ""
    if created:
        from datetime import datetime
        when = datetime.fromtimestamp(created).strftime("%Y-%m-%d")
    byline_session = contract.get("byline_session_id") or "?"
    when_str = f" since {when}" if when else ""
    return (
        f"[lcc.contract] {kind} {body}. Active{when_str}, "
        f"byline session {_sanitize_for_context(byline_session, max_len=20)}. "
        f'Expand: call MCP tool \'lcc_contracts\' with {{"action": "show", "id": "{cid}"}}'
    )


def _render_handoff_ref(session: dict) -> str:
    """Render a session's handoff as a one-line ref + Expand pointer."""
    handoff = (session.get("handoff_text") or "").strip()
    if not handoff:
        return ""
    sid = session.get("session_id", "?")
    summary_line = _sanitize_for_context(handoff.split("\n", 1)[0], max_len=200)
    return (
        f"[lcc.handoff] {summary_line}. "
        f'Expand: call MCP tool \'lcc_handoff\' with {{"session_id": "{sid}"}}'
    )


def _render_decision_ref(summary: dict) -> str:
    """Render a decision-typed summary as a one-line ref + Expand pointer."""
    body = (summary.get("content") or "").strip()
    if not body:
        return ""
    summary_line = _sanitize_for_context(body.split("\n", 1)[0], max_len=200)
    sid = summary.get("id", "?")
    session_id = summary.get("session_id") or "?"
    created = summary.get("created_at")
    when = ""
    if created:
        from datetime import datetime
        when = datetime.fromtimestamp(created).strftime("%Y-%m-%d")
    when_str = f" dated {when}" if when else ""
    return (
        f"[lcc.decision] {summary_line} (session "
        f"{_sanitize_for_context(session_id, max_len=20)}{when_str}). "
        f'Expand: call MCP tool \'lcc_expand\' with {{"summary_id": "{sid}"}}'
    )


def _pack_slot(items: list[dict], slot_budget: int, renderer) -> tuple[list[str], int]:
    """Greedy-pack rendered items into a single slot.

    Returns (rendered_lines, tokens_used). Items that exceed the slot
    budget after rendering are skipped rather than emitted truncated;
    each renderer is responsible for keeping its output below the
    per-item ceiling. Empty rendered output (sanitization rejection,
    missing fields) is treated as a skip.
    """
    out: list[str] = []
    used = 0
    for item in items:
        line = renderer(item)
        if not line:
            continue
        line_tokens = estimate_tokens(line) + 1  # +1 for the joining newline
        if used + line_tokens > slot_budget:
            break
        out.append(line)
        used += line_tokens
    return out, used


def _list_active_contracts(working_dir: str, limit: int = 50) -> list[dict]:
    """Return Active contracts scoped to project + global. Newest first.

    Project-scoped contracts come first, then up to 2 global entries from
    `dream/global/` (D8 mirroring). Combined limit is bounded so the
    bundle assembler does not load an unbounded list.
    """
    project_rows = db.list_contracts(status="Active", scope="project", limit=limit)
    global_rows = db.list_contracts(status="Active", scope="global", limit=2)
    return list(project_rows) + list(global_rows)[:2]


def _list_recent_decisions(working_dir: str, limit: int = 20) -> list[dict]:
    """Return decision-typed summaries scoped to cwd, newest first."""
    conn = db.get_db()
    rows = conn.execute(
        "SELECT s.* FROM summaries s "
        "LEFT JOIN sessions sess ON s.session_id = sess.session_id "
        "WHERE s.kind = 'decision' "
        "  AND (? = '' OR sess.working_dir = ?) "
        "ORDER BY s.created_at DESC LIMIT ?",
        (working_dir or "", working_dir or "", limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _list_active_file_fingerprints(working_dir: str, limit: int = 10) -> list[tuple[str, list[dict]]]:
    """Return (file_path, summaries) tuples for files with prior activity
    in the cwd. Each entry is rendered via format_file_fingerprint."""
    if not working_dir:
        return []
    conn = db.get_db()
    rows = conn.execute(
        "SELECT DISTINCT file_path FROM messages "
        "WHERE working_dir = ? AND file_path IS NOT NULL "
        "ORDER BY timestamp DESC LIMIT ?",
        (working_dir, limit),
    ).fetchall()
    out = []
    for r in rows:
        fp = r["file_path"]
        if not fp:
            continue
        summaries = db.get_summaries_for_file(fp, limit=3)
        if summaries:
            out.append((fp, summaries))
    return out


def _pack_fingerprint_slot(
    items: list[tuple[str, list[dict]]], slot_budget: int
) -> tuple[list[str], int]:
    """Specialised packer for file fingerprints which use the existing
    format_file_fingerprint renderer (file_path + summaries pair)."""
    out: list[str] = []
    used = 0
    for file_path, summaries in items:
        # Per-fingerprint cap: ~50 tokens via format_file_fingerprint.
        line = format_file_fingerprint(file_path, summaries, token_budget=50)
        if not line:
            continue
        line_tokens = estimate_tokens(line) + 1
        if used + line_tokens > slot_budget:
            break
        out.append(line)
        used += line_tokens
    return out, used


def build_context(
    session_id: str = None,
    working_dir: str = "",
    query: str = "",
    limit: int = 5,
    config_override: dict = None,
) -> str:
    """Assemble the v1.2 reference bundle for SessionStart injection.

    The bundle is a token-budgeted reference set; the agent pulls depth on
    demand via MCP tools (lcc_expand, lcc_grep, lcc_context). The legacy
    full-text injection from v1.1 has been removed; set bundleEnabled=false
    in config to opt out of injection entirely (graceful degradation).

    Slot order (TD8): header + recovery line first, then contracts,
    handoff ref, decisions, file fingerprints. The recovery line and
    header are last to drop under R16 budget pressure; everything else
    drops oldest-first per the slot ladder.
    """
    config = {**db.load_config(), **(config_override or {})}

    # Rollback escape (TD9). When disabled, emit nothing; SessionStart
    # hook then exits without additionalContext.
    if not config.get("bundleEnabled", True):
        return ""

    total_budget = int(config.get("bundleTokenBudget", _DEFAULT_BUNDLE_BUDGET))

    # Recovery line and header are always emitted. Their cost is
    # subtracted from total_budget before slot packing.
    recovery_line = _render_recovery_line()
    fixed_tokens = (
        estimate_tokens(_BUNDLE_HEADER)
        + estimate_tokens(recovery_line)
        + 4  # newlines / blank-line separators
    )
    available = max(0, total_budget - fixed_tokens)

    # Pack each slot in order. Each slot has a hard cap; if the total
    # remaining budget runs out, later slots get less than their nominal
    # allocation (R16 drop order: contracts and recovery line are last
    # to lose space).
    contracts = _list_active_contracts(working_dir)
    contract_lines, contract_used = _pack_slot(
        contracts,
        min(_SLOT_BUDGETS["contracts"], available),
        _render_contract_ref,
    )
    available -= contract_used

    handoff_lines: list[str] = []
    handoff_used = 0
    if available > 0:
        handoff_session = _get_handoff_session(session_id)
        if handoff_session:
            handoff_lines, handoff_used = _pack_slot(
                [handoff_session],
                min(_SLOT_BUDGETS["handoff"], available),
                _render_handoff_ref,
            )
            available -= handoff_used

    decision_lines: list[str] = []
    if available > 0:
        decisions = _list_recent_decisions(working_dir)
        decision_lines, used = _pack_slot(
            decisions,
            min(_SLOT_BUDGETS["decisions"], available),
            _render_decision_ref,
        )
        available -= used

    fingerprint_lines: list[str] = []
    if available > 0:
        fingerprints = _list_active_file_fingerprints(working_dir)
        fingerprint_lines, used = _pack_fingerprint_slot(
            fingerprints,
            min(_SLOT_BUDGETS["fingerprints"], available),
        )
        available -= used

    # Bail out cleanly when there is no content beyond header + recovery
    # line. Empty vault on first install: bundle still ships header +
    # recovery line so the agent learns the protocol.
    parts: list[str] = [_BUNDLE_HEADER, recovery_line]
    if contract_lines:
        parts.append("\n".join(contract_lines))
    if handoff_lines:
        parts.append("\n".join(handoff_lines))
    if decision_lines:
        parts.append("\n".join(decision_lines))
    if fingerprint_lines:
        parts.append("\n".join(fingerprint_lines))
    return "\n\n".join(parts)


def _get_handoff_session(session_id: str | None) -> dict | None:
    """Return the most recent session row with a handoff, optionally
    matching session_id when supplied. Used by the bundle's handoff slot.
    """
    if session_id:
        s = db.get_session(session_id)
        if s and s.get("handoff_text"):
            return s
    sessions = db.list_sessions(limit=10)
    for s in sessions:
        if s.get("handoff_text"):
            return s
    return None


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
