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


def _active_session_id() -> str | None:
    return os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("CODEX_SESSION_ID") or None


def _format_timestamp(timestamp) -> str:
    try:
        value = float(timestamp)
    except (TypeError, ValueError):
        value = 0
    if value > 10_000_000_000:
        value = value / 1000
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))

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
                    "description": (
                        "Show full per-item content without per-line truncation "
                        "(default false). The total response size is always "
                        "capped; a chain that exceeds the budget returns an "
                        "expand_too_large structured error regardless of full."
                    ),
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
            "Return the v1.2 reference bundle for the current session: "
            "active contracts, the latest handoff line, recent decisions, "
            "and file fingerprints, each with an Expand instruction. Pair "
            "with lcc_grep when you need topic-search over recent turns."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
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
        name="lcc_contracts",
        description=(
            "Behavior-contract registry: typed retractable rules the agent "
            "follows next session. Actions:\n"
            "- list: return rows filtered by status (Pending|Active|Retracted|Rejected) and scope.\n"
            "- show: return a single row by id.\n"
            "- approve: promote a Pending row to Active.\n"
            "- reject: mark a Pending row as Rejected.\n"
            "- retract: flip an Active row to Retracted (requires reason).\n"
            "- supersede: atomically replace an Active row with a new Active row (requires body).\n"
            "\n"
            "Errors return as JSON "
            '`{\"error\": {\"code\": \"<code>\", \"message\": \"<static>\"}}` '
            "with codes contract_not_found, invalid_action, missing_argument, "
            "duplicate_body, invalid_status. Message strings are static and "
            "never expose filesystem paths or exception internals (TD7)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "show", "approve", "reject", "retract", "supersede"],
                    "description": "What to do.",
                },
                "id": {
                    "type": "string",
                    "description": "Contract id, required for show/approve/reject/retract/supersede.",
                },
                "status": {
                    "type": "string",
                    "enum": ["Pending", "Active", "Retracted", "Rejected"],
                    "description": "Filter for list. Defaults to Pending.",
                },
                "scope": {
                    "type": "string",
                    "description": "Filter for list. Optional.",
                },
                "reason": {
                    "type": "string",
                    "description": "Required for retract action.",
                },
                "body": {
                    "type": "string",
                    "description": "New contract body, required for supersede action.",
                },
                "byline_session_id": {
                    "type": "string",
                    "description": "Optional byline metadata for supersede.",
                },
                "byline_model": {
                    "type": "string",
                    "description": "Optional byline metadata for supersede.",
                },
            },
            "required": ["action"],
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
            ts = _format_timestamp(m["timestamp"])
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
    "contract_not_found": "contract not found",
    "invalid_action": "action must be one of list, show, approve, reject, retract, supersede",
    "missing_argument": "required argument missing for the requested action",
    "duplicate_body": "contract body is a duplicate of an existing entry",
    "invalid_status": "status must be one of Pending, Active, Retracted, Rejected",
    "internal_error": "internal error in tool handler",
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
        ts = _format_timestamp(span["timestamp"])
        kind = span.get("span_kind") or "?"
        content = span["content"]
        if len(content) > 500 and not full:
            content = content[:500] + "..."
        line = f"[hop {span['hop']}] {ts} ({kind}, id={span['id']}) {content}\n"
        running += len(line)
        # The chain-total cap is a hard ceiling on agent-context cost.
        # full=true relaxes per-span truncation only, never the total.
        # Without this, full=true is an unbounded read primitive: a long
        # chain (legitimately or maliciously long) could push tens of
        # thousands of characters into a single tool result.
        if running > _SPAN_EXPAND_MAX_CHARS:
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
                ts = _format_timestamp(m["timestamp"])
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


def _do_context() -> str:
    context = inject_context.build_context(
        session_id=_active_session_id(),
        working_dir=os.getcwd(),
    )
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
    """Routes through lcc_core.collect_status_dict so the MCP surface
    reports identical fields to the CLI (U13)."""
    import lcc_core
    return lcc_core.format_status_human(lcc_core.collect_status_dict())


_VALID_CONTRACT_ACTIONS = {"list", "show", "approve", "reject", "retract", "supersede"}
_VALID_CONTRACT_STATUSES = {"Pending", "Active", "Retracted", "Rejected"}


def _format_contract_row(row: dict) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(row.get("created_at", 0)))
    parts = [
        f"id: {row.get('id')}",
        f"kind: {row.get('kind')}",
        f"status: {row.get('status')}",
        f"created: {ts}",
        f"scope: {row.get('scope')}",
    ]
    if row.get("byline_session_id"):
        parts.append(f"byline_session: {row['byline_session_id']}")
    if row.get("byline_model"):
        parts.append(f"byline_model: {row['byline_model']}")
    if row.get("supersedes_id"):
        parts.append(f"supersedes: {row['supersedes_id']}")
    if row.get("conflicts_with"):
        parts.append(f"conflicts_with: {row['conflicts_with']}")
    parts.append("body:")
    parts.append(row.get("body", ""))
    return "\n".join(parts)


def _do_contracts(args: dict) -> str:
    """Dispatch the lcc_contracts MCP tool. Returns rendered text on
    success, or a JSON structured error per TD7."""
    action = args.get("action")
    if action not in _VALID_CONTRACT_ACTIONS:
        return _structured_error("invalid_action")

    if action == "list":
        status = args.get("status", "Pending")
        if status not in _VALID_CONTRACT_STATUSES:
            return _structured_error("invalid_status")
        scope = args.get("scope") or None
        rows = db.list_contracts(status=status, scope=scope)
        if not rows:
            return f"No contracts in status={status}" + (
                f" scope={scope}" if scope else ""
            )
        out = [f"=== Contracts ({status}, {len(rows)} rows) ==="]
        for r in rows:
            out.append("")
            out.append(_format_contract_row(r))
        return "\n".join(out)

    cid = args.get("id")
    if not cid:
        return _structured_error("missing_argument")

    if action == "show":
        row = db.get_contract(cid)
        if row is None:
            return _structured_error("contract_not_found")
        return _format_contract_row(row)

    if action == "approve":
        ok = db.approve_contract(cid)
        if not ok:
            return _structured_error("contract_not_found")
        return f"approved {cid}"

    if action == "reject":
        ok = db.reject_contract(cid)
        if not ok:
            return _structured_error("contract_not_found")
        return f"rejected {cid}"

    if action == "retract":
        reason = args.get("reason")
        if not reason:
            return _structured_error("missing_argument")
        ok = db.retract_contract(cid, reason=reason)
        if not ok:
            return _structured_error("contract_not_found")
        return f"retracted {cid}"

    if action == "supersede":
        body = args.get("body")
        if not body:
            return _structured_error("missing_argument")
        new_id = db.supersede_contract(
            cid,
            new_body=body,
            byline_session_id=args.get("byline_session_id"),
            byline_model=args.get("byline_model"),
        )
        if new_id is None:
            # Either the target was missing or the new body was a duplicate.
            row = db.get_contract(cid)
            if row is None:
                return _structured_error("contract_not_found")
            return _structured_error("duplicate_body")
        return f"superseded {cid} -> {new_id}"

    # Should be unreachable given the action check above.
    return _structured_error("invalid_action")


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
            text = _do_context()
        elif name == "lcc_sessions":
            text = _do_sessions(limit=arguments.get("limit", 20))
        elif name == "lcc_handoff":
            text = _do_handoff(session_id=arguments.get("session_id", ""))
        elif name == "lcc_contracts":
            text = _do_contracts(arguments)
        elif name == "lcc_status":
            text = _do_status()
        else:
            text = f"Unknown tool: {name}"
    except Exception as e:
        # Never leak raw exception strings into agent context. They carry
        # filesystem paths, SQLite library internals, and stack-frame data
        # that becomes load-bearing on the agent's next turn. Per TD7, every
        # tool error returns a structured-error JSON with a static message.
        # Operators get the real cause via stderr.
        print(
            f"[lcc-mcp] {name} raised {type(e).__name__}: {e}",
            file=sys.stderr,
            flush=True,
        )
        text = _structured_error("internal_error")

    return [TextContent(type="text", text=text)]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
