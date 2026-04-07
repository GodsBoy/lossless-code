#!/usr/bin/env python3
"""
MCP server for lossless-code.

Exposes the vault as MCP tools for Claude Code: grep, expand, context,
sessions, handoff, and status.  Read-only — hooks handle all writes.

Transport: stdio (stdin/stdout JSON-RPC).
"""

import asyncio
import os
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
            "Expand a summary ID back to its source messages and child "
            "summaries. Traverses the DAG to show what was compressed."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "summary_id": {
                    "type": "string",
                    "description": "Summary ID to expand (e.g. sum_abc123def456)",
                },
                "full": {
                    "type": "boolean",
                    "description": "Show full content without truncation (default false)",
                    "default": False,
                },
            },
            "required": ["summary_id"],
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
            text = _do_expand(
                summary_id=arguments["summary_id"],
                full=arguments.get("full", False),
            )
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
