#!/usr/bin/env python3
"""
MCP server for lossless-code.

Exposes the vault as MCP tools for Claude Code: grep, expand, context,
sessions, handoff, and status.  Read-only — hooks handle all writes.

Transport: stdio (stdin/stdout JSON-RPC).
"""

import asyncio
import json
import os
import sqlite3
import sys
import time

# Ensure scripts/ is importable
SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
if os.path.isdir(SCRIPTS_DIR):
    sys.path.insert(0, SCRIPTS_DIR)
else:
    # Installed location: ~/.lossless-code/mcp/ with scripts at ~/.lossless-code/scripts/
    SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
    installed_scripts = os.path.normpath(SCRIPTS_DIR)
    if os.path.isdir(installed_scripts):
        sys.path.insert(0, installed_scripts)

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

import db
import inject_context
import summarise as summarise_mod

# ---------------------------------------------------------------------------
# Override vault path if env var is set
# ---------------------------------------------------------------------------

vault_env = os.environ.get("LOSSLESS_CODE_VAULT")
if vault_env:
    from pathlib import Path
    db.VAULT_DB = Path(vault_env)
    db.LOSSLESS_HOME = Path(vault_env).parent

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

server = Server("lossless-code")


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    Tool(
        name="lcc_grep",
        description=(
            "Full-text search across all messages and summaries in the "
            "lossless-code vault. Returns matching conversation fragments "
            "and summary nodes ranked by relevance."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query (words or phrases)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results per category (default 20)",
                    "default": 20,
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="lcc_expand",
        description=(
            "Expand a stored node back to its sources. Three input modes:\n"
            "1. `summary_id`: expand a summary back to its source messages and "
            "child summaries (DAG traversal).\n"
            "2. `file`: list the most recent summaries linked to a file path "
            "(requires fileContextEnabled).\n"
            "3. `span_id`: walk the message-to-message parent chain upward from "
            "the given message id, returning all ancestors. Used when a bundle "
            "reference points at a span (v1.2+).\n"
            "\n"
            "On `span_id` mode only, errors are returned as JSON "
            '`{\"error\": {\"code\": \"<code>\", \"message\": \"<static>\"}}` '
            "where `code` is one of: span_not_found, expand_too_large, "
            "vault_corrupt. Agent fallback when expand_too_large: call "
            "lcc_grep or lcc_context with topic terms drawn from the failed "
            "reference. Other modes (summary_id, file) preserve the v1.1.x "
            "human-readable error format."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "summary_id": {
                    "type": "string",
                    "description": "Summary ID to expand (e.g. sum_abc123def456)",
                },
                "file": {
                    "type": "string",
                    "description": "File path to expand. Returns recent summaries that mention it.",
                },
                "span_id": {
                    "type": "string",
                    "description": (
                        "Message ID (integer, accepted as string) to walk "
                        "upward from via parent_message_id. v1.2+."
                    ),
                },
                "full": {
                    "type": "boolean",
                    "description": "Show full content without truncation (default false)",
                    "default": False,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max summaries when expanding by file (default 3)",
                    "default": 3,
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="lcc_context",
        description=(
            "Get relevant context for a query. Combines search with "
            "handoff text and top summaries from the DAG. Use this to "
            "recall what happened in previous sessions."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Query to find relevant context (optional)",
                    "default": "",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max summary nodes to include (default 5)",
                    "default": 5,
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="lcc_sessions",
        description=(
            "List sessions in the vault with metadata: start time, "
            "last active, working directory, and whether a handoff exists."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max sessions to list (default 20)",
                    "default": 20,
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="lcc_handoff",
        description=(
            "Get the handoff document from a session. Shows what was "
            "worked on, key decisions, current state, and next steps."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID (optional — defaults to most recent with handoff)",
                    "default": "",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="lcc_status",
        description=(
            "Show vault statistics: session count, message count, "
            "summary count, max DAG depth, and database file size."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
]


@server.list_tools()
async def list_tools():
    return TOOLS


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _do_grep(query: str, limit: int = 20) -> str:
    results = db.search_all(query, limit=limit)
    msgs = results["messages"]
    sums = results["summaries"]

    if not msgs and not sums:
        return f"No results for: {query}"

    parts = []

    if msgs:
        parts.append(f"=== Messages ({len(msgs)} matches) ===\n")
        for m in msgs:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(m["timestamp"]))
            role = m["role"]
            content = m["content"]
            if len(content) > 200:
                content = content[:200] + "..."
            parts.append(f"[{ts}] ({role}) {content}\n")

    if sums:
        parts.append(f"=== Summaries ({len(sums)} matches) ===\n")
        for s in sums:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["created_at"]))
            depth = s["depth"]
            content = s["content"]
            if len(content) > 300:
                content = content[:300] + "..."
            parts.append(f"[{ts}] (depth-{depth}, {s['id']}) {content}\n")

    return "\n".join(parts)


def _do_expand_file(file_path: str, limit: int = 3, full: bool = False) -> str:
    cfg = db.load_config()
    if not cfg.get("fileContextEnabled", False):
        return "lcc_expand by file requires fileContextEnabled=true in config."
    summaries = db.get_summaries_for_file(file_path, limit=limit)
    if not summaries:
        return f"No summaries reference {file_path}."
    parts = [f"=== Summaries for {file_path} ({len(summaries)}) ==="]
    for s in summaries:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["created_at"]))
        kind = s.get("kind") or "?"
        content = s["content"]
        if len(content) > 500 and not full:
            content = content[:500] + "..."
        parts.append(
            f"[{ts}] (depth-{s['depth']}, {kind}, {s['id']}) {content}\n"
        )
    return "\n".join(parts)


# v1.2+ structured-error helper. Static messages ONLY, never str(exception).
# Raw exception strings leak filesystem paths, library internals, and schema
# fragments into agent context, so the message field is always a pre-defined
# constant. The code field is the load-bearing semantic signal.
_STRUCTURED_ERROR_MESSAGES = {
    "span_not_found": "span not found",
    "expand_too_large": "span chain exceeds expansion budget; try lcc_grep instead",
    "vault_corrupt": "vault unreachable",
    "permission_denied": "permission denied",
}


def _structured_error(code: str) -> str:
    """Return a JSON-encoded structured error per TD7."""
    return json.dumps(
        {
            "error": {
                "code": code,
                "message": _STRUCTURED_ERROR_MESSAGES.get(code, "internal error"),
            }
        },
        separators=(",", ":"),
    )


# Soft cap on span-chain expansion size, enforced before render. 8000 chars
# is roughly 2000 tokens, well below the typical recoveryFetchCost target
# but big enough to carry a useful causal chain.
_SPAN_EXPAND_MAX_CHARS = 8000


def _do_expand_span(span_id: str, full: bool = False) -> str:
    """Walk the parent chain from the given message id. Returns rendered
    text on success, or a JSON structured error on failure (TD7)."""
    try:
        msg_id = int(span_id)
    except (TypeError, ValueError):
        return _structured_error("span_not_found")
    try:
        chain = db.get_span_chain(msg_id)
    except sqlite3.DatabaseError:
        return _structured_error("vault_corrupt")
    if not chain:
        return _structured_error("span_not_found")

    parts = [f"=== Span chain for message {msg_id} ({len(chain)} hops) ==="]
    running = len(parts[0])
    for span in chain:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(span["timestamp"]))
        kind = span.get("span_kind") or "?"
        content = span["content"]
        if len(content) > 500 and not full:
            content = content[:500] + "..."
        line = f"[hop {span['hop']}] {ts} ({kind}, id={span['id']}) {content}\n"
        running += len(line)
        if running > _SPAN_EXPAND_MAX_CHARS and not full:
            return _structured_error("expand_too_large")
        parts.append(line)
    return "\n".join(parts)


def _do_expand(summary_id: str, full: bool = False) -> str:
    summary = db.get_summary(summary_id)
    if not summary:
        return f"Summary not found: {summary_id}"

    parts = [
        f"=== Summary {summary_id} (depth {summary['depth']}) ===",
        summary["content"],
        "",
    ]

    sources = db.get_summary_sources(summary_id)
    if not sources:
        parts.append("No sources linked.")
        return "\n".join(parts)

    parts.append(f"=== Sources ({len(sources)}) ===\n")

    for src in sources:
        if src["source_type"] == "message":
            msgs = db.get_messages_by_ids([src["source_id"]])
            for m in msgs:
                ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(m["timestamp"]))
                content = m["content"]
                if len(content) > 500 and not full:
                    content = content[:500] + "..."
                parts.append(f"[{ts}] ({m['role']}) {content}\n")
        elif src["source_type"] == "summary":
            child = db.get_summary(src["source_id"])
            if child:
                content = child["content"]
                if len(content) > 500 and not full:
                    content = content[:500] + "..."
                parts.append(f"[summary {child['id']}, depth-{child['depth']}]")
                parts.append(f"{content}\n")

    return "\n".join(parts)


def _do_context(query: str = "", limit: int = 5) -> str:
    context = inject_context.build_context(query=query, limit=limit)
    return context if context else "No context available yet."


def _do_sessions(limit: int = 20) -> str:
    sessions = db.list_sessions(limit=limit)
    if not sessions:
        return "No sessions recorded yet."

    lines = []
    for s in sessions:
        started = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(s["started_at"]))
            if s["started_at"]
            else "?"
        )
        last = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(s["last_active"]))
            if s["last_active"]
            else "?"
        )
        handoff = "yes" if s.get("handoff_text") else "no"
        wdir = s.get("working_dir", "")
        msg_count = db.count_session_messages(s["session_id"])
        lines.append(
            f"  {s['session_id'][:16]}...  started={started}  last={last}  "
            f"msgs={msg_count}  handoff={handoff}  dir={wdir}"
        )

    return "\n".join(lines)


def _do_handoff(session_id: str = "") -> str:
    handoff = inject_context.get_handoff(session_id or None)
    return handoff if handoff else "No handoff available."


def _do_status() -> str:
    d = db.get_db()
    msg_count = d.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    sum_count = d.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
    ses_count = d.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    unsummarised = d.execute(
        "SELECT COUNT(*) FROM messages WHERE summarised = 0"
    ).fetchone()[0]
    max_depth = d.execute(
        "SELECT COALESCE(MAX(depth), 0) FROM summaries"
    ).fetchone()[0]

    vault_size = os.path.getsize(str(db.VAULT_DB)) if db.VAULT_DB.exists() else 0
    vault_mb = vault_size / (1024 * 1024)

    cfg = db.load_config()
    fp_line = ""
    if cfg.get("fileContextEnabled", False):
        tagged = d.execute(
            "SELECT COUNT(*) FROM messages WHERE file_path IS NOT NULL"
        ).fetchone()[0]
        distinct = d.execute(
            "SELECT COUNT(DISTINCT file_path) FROM messages "
            "WHERE file_path IS NOT NULL"
        ).fetchone()[0]
        cache_count = 0
        try:
            import file_context as fc
            cache_count = len(fc._load_cache())
        except Exception:
            pass
        fp_line = (
            f"\n  Fingerprint:   {tagged} tagged messages across "
            f"{distinct} files ({cache_count} cached)"
        )

    # Provider info (MCP parity with CLI status)
    pinfo = summarise_mod.get_provider_info()
    p_name = pinfo.get("provider") or "none"
    p_model = pinfo.get("model") or "none"
    p_suffix = " via auto-detect" if pinfo.get("auto_detected") else ""
    p_err = pinfo.get("last_error") or "none"

    return (
        f"lossless-code vault status\n"
        f"  Vault:         {db.VAULT_DB} ({vault_mb:.2f} MB)\n"
        f"  Sessions:      {ses_count}\n"
        f"  Messages:      {msg_count} ({unsummarised} unsummarised)\n"
        f"  Summaries:     {sum_count} (max depth: {max_depth})\n"
        f"  Provider:      {p_name} ({p_model}){p_suffix}\n"
        f"               Last error: {p_err}"
        f"{fp_line}"
    )


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        if name == "lcc_grep":
            text = _do_grep(
                query=arguments["query"],
                limit=arguments.get("limit", 20),
            )
        elif name == "lcc_expand":
            if arguments.get("file"):
                text = _do_expand_file(
                    file_path=arguments["file"],
                    limit=arguments.get("limit", 3),
                    full=arguments.get("full", False),
                )
            elif arguments.get("summary_id"):
                text = _do_expand(
                    summary_id=arguments["summary_id"],
                    full=arguments.get("full", False),
                )
            elif arguments.get("span_id"):
                text = _do_expand_span(
                    span_id=arguments["span_id"],
                    full=arguments.get("full", False),
                )
            else:
                text = "lcc_expand requires `summary_id`, `file`, or `span_id`."
        elif name == "lcc_context":
            text = _do_context(
                query=arguments.get("query", ""),
                limit=arguments.get("limit", 5),
            )
        elif name == "lcc_sessions":
            text = _do_sessions(limit=arguments.get("limit", 20))
        elif name == "lcc_handoff":
            text = _do_handoff(session_id=arguments.get("session_id", ""))
        elif name == "lcc_status":
            text = _do_status()
        else:
            text = f"Unknown tool: {name}"
    except Exception as e:
        text = f"Error in {name}: {e}"

    return [TextContent(type="text", text=text)]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
