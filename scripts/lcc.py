#!/usr/bin/env python3
"""
lcc — Lossless Context Commands for Claude Code.

Subcommands:
  grep      Full-text search across all messages and summaries
  expand    Expand a summary node back to its source messages
  context   Surface top N relevant DAG nodes for a query
  sessions  List sessions with metadata and handoff text
  handoff   Show or generate handoff for a session
  status    Show vault stats
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
import codex_support
import embed as embed_mod
import inject_context
import summarise as summarise_mod


def _active_session_id():
    return os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("CODEX_SESSION_ID") or None


def _format_timestamp(timestamp):
    try:
        value = float(timestamp)
    except (TypeError, ValueError):
        value = 0
    if value > 10_000_000_000:
        value = value / 1000
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))


def cmd_grep(args):
    """Full-text search across messages and summaries (hybrid when embedding active)."""
    cfg = db.load_config()
    results = embed_mod.hybrid_search(args.query, cfg, limit=args.limit)

    msgs = results["messages"]
    sums = results["summaries"]
    is_hybrid = results.get("hybrid", False)
    mode_tag = " [hybrid]" if is_hybrid else ""

    if not msgs and not sums:
        print(f"No results for: {args.query}")
        return

    if msgs:
        print(f"=== Messages ({len(msgs)} matches{mode_tag}) ===\n")
        for m in msgs:
            ts = _format_timestamp(m["timestamp"])
            role = m["role"]
            content = m["content"]
            if len(content) > 200:
                content = content[:200] + "..."
            print(f"[{ts}] ({role}) {content}\n")

    if sums:
        print(f"=== Summaries ({len(sums)} matches) ===\n")
        for s in sums:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["created_at"]))
            depth = s["depth"]
            content = s["content"]
            if len(content) > 300:
                content = content[:300] + "..."
            print(f"[{ts}] (depth-{depth}, {s['id']}) {content}\n")


def cmd_expand(args):
    """Expand a summary node to its source messages/summaries."""
    if getattr(args, "span_id", None):
        cmd_expand_span(args)
        return
    if getattr(args, "file", None):
        cfg = db.load_config()
        if not cfg.get("fileContextEnabled", False):
            print("lcc expand --file requires fileContextEnabled=true in config.")
            return
        summaries = db.get_summaries_for_file(args.file, limit=args.limit)
        if not summaries:
            print(f"No summaries reference {args.file}.")
            return
        print(f"=== Summaries for {args.file} ({len(summaries)}) ===\n")
        for s in summaries:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["created_at"]))
            kind = s.get("kind") or "?"
            content = s["content"]
            if len(content) > 500 and not args.full:
                content = content[:500] + "..."
            print(f"[{ts}] (depth-{s['depth']}, {kind}, {s['id']}) {content}\n")
        return

    if not args.summary_id:
        print("Provide a summary_id or --file <path>.")
        return
    summary = db.get_summary(args.summary_id)
    if not summary:
        print(f"Summary not found: {args.summary_id}")
        return

    print(f"=== Summary {args.summary_id} (depth {summary['depth']}) ===")
    print(summary["content"])
    print()

    sources = db.get_summary_sources(args.summary_id)
    if not sources:
        print("No sources linked.")
        return

    print(f"=== Sources ({len(sources)}) ===\n")
    for src in sources:
        if src["source_type"] == "message":
            msgs = db.get_messages_by_ids([src["source_id"]])
            for m in msgs:
                ts = _format_timestamp(m["timestamp"])
                content = m["content"]
                if len(content) > 500 and not args.full:
                    content = content[:500] + "..."
                print(f"[{ts}] ({m['role']}) {content}\n")
        elif src["source_type"] == "summary":
            child = db.get_summary(src["source_id"])
            if child:
                print(f"[summary {child['id']}, depth-{child['depth']}]")
                content = child["content"]
                if len(content) > 500 and not args.full:
                    content = content[:500] + "..."
                print(f"{content}\n")


def cmd_expand_span(args):
    """Walk the parent chain from a message id (CLI parity with the MCP
    span_id mode). Mirrors the per-line truncation rules but skips the
    chain-total cap because CLI consumers are humans, not agent context.
    """
    try:
        msg_id = int(args.span_id)
    except (TypeError, ValueError):
        print(f"Invalid span id: {args.span_id}")
        return
    chain = db.get_span_chain(msg_id)
    if not chain:
        print(f"No span chain rooted at message {msg_id}.")
        return
    print(f"=== Span chain for message {msg_id} ({len(chain)} hops) ===")
    for span in chain:
        ts = _format_timestamp(span["timestamp"])
        kind = span.get("span_kind") or "?"
        content = span["content"]
        if len(content) > 500 and not args.full:
            content = content[:500] + "..."
        print(f"[hop {span['hop']}] {ts} ({kind}, id={span['id']}) {content}\n")


def cmd_context(args):
    """Print the v1.2 reference bundle that SessionStart would inject."""
    context = inject_context.build_context(
        session_id=_active_session_id(),
        working_dir=os.getcwd(),
    )
    if context:
        print(context)
    else:
        print("No context available yet.")


def cmd_sessions(args):
    """List sessions with metadata."""
    sessions = db.list_sessions(limit=args.limit)
    if not sessions:
        print("No sessions recorded yet.")
        return

    for s in sessions:
        started = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["started_at"])) if s["started_at"] else "?"
        last = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["last_active"])) if s["last_active"] else "?"
        handoff = "yes" if s.get("handoff_text") else "no"
        wdir = s.get("working_dir", "")
        print(f"  {s['session_id'][:16]}...  started={started}  last={last}  handoff={handoff}  dir={wdir}")


def cmd_handoff(args):
    """Show or generate handoff for a session."""
    if args.generate:
        # Generate handoff from recent messages
        session_id = args.session or os.environ.get("CLAUDE_SESSION_ID", "")
        if not session_id:
            print("No session ID provided. Use --session or set CLAUDE_SESSION_ID.")
            return

        session = db.get_session(session_id)
        if not session:
            print(f"Session not found: {session_id}")
            return

        # Get recent messages from this session
        all_db = db.get_db()
        rows = all_db.execute(
            """SELECT * FROM messages WHERE session_id = ?
               ORDER BY timestamp DESC LIMIT 30""",
            (session_id,),
        ).fetchall()
        messages = [dict(r) for r in reversed(rows)]

        if not messages:
            print("No messages in this session to generate handoff from.")
            return

        # Build handoff text (use handoffModel if set, fallback to summaryModel)
        cfg = db.load_config()
        handoff_cfg = dict(cfg)
        handoff_model = cfg.get("handoffModel")
        if handoff_model:
            handoff_cfg["summaryModel"] = handoff_model
        text = summarise_mod.format_messages_for_summary(messages)
        prompt_text = (
            "Generate a concise handoff summary for the next coding session. "
            "Include: what was worked on, key decisions made, current state, "
            "what needs to happen next. Be specific with file paths and commands.\n\n"
            f"{text}"
        )
        handoff_text = summarise_mod.call_summary_model(prompt_text, handoff_cfg)
        db.set_handoff(session_id, handoff_text)
        print(f"Handoff generated and saved for session {session_id[:16]}...")
        print(f"\n{handoff_text}")
    else:
        # Show existing handoff
        handoff = inject_context.get_handoff(args.session)
        if handoff:
            print(handoff)
        else:
            print("No handoff available. Use --generate to create one.")


def cmd_summarise(args):
    """Run compaction (summarisation) on vault messages."""
    result = summarise_mod.run_full_summarisation(args.session)
    print(json.dumps(result, indent=2))


def cmd_status(args):
    """Show vault statistics. Routes through lcc_core.collect_status_dict
    so the CLI and MCP surfaces report identical fields (U13)."""
    import lcc_core
    status = lcc_core.collect_status_dict(working_dir=os.getcwd())
    print(lcc_core.format_status_human(status))


def cmd_reindex(args):
    """Embed un-indexed messages for hybrid search."""
    cfg = db.load_config()
    if args.model:
        cfg = {**cfg, "embeddingModel": args.model}
    if not cfg.get("embeddingEnabled", False) and not args.model:
        print("Embedding is disabled. Set embeddingEnabled: true in config or pass --model.")
        print("Config path:", db.CONFIG_PATH)
        return
    # Enable embedding temporarily for this run
    cfg["embeddingEnabled"] = True
    embed_mod.reindex_vault(cfg, force=args.force, model_override=args.model)


def cmd_dream(args):
    """Run dream cycle — extract patterns and consolidate DAG."""
    import dream as dream_mod
    scope = "global" if args.global_scope else "project"
    working_dir = args.project or os.getcwd()
    cfg = db.load_config()
    report = dream_mod.run_dream(scope, working_dir, cfg)
    print(report)


def _print_contract_row(row: dict) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(row.get("created_at", 0)))
    print(f"id:     {row['id']}")
    print(f"kind:   {row['kind']}")
    print(f"status: {row['status']}")
    print(f"created: {ts}")
    print(f"scope:  {row.get('scope')}")
    if row.get("byline_session_id"):
        print(f"byline_session: {row['byline_session_id']}")
    if row.get("byline_model"):
        print(f"byline_model:   {row['byline_model']}")
    if row.get("supersedes_id"):
        print(f"supersedes:     {row['supersedes_id']}")
    if row.get("conflicts_with"):
        print(f"conflicts_with: {row['conflicts_with']}")
    print("body:")
    print(row.get("body", ""))


def cmd_contracts(args):
    """Behavior-contract registry CLI. Mirrors the lcc_contracts MCP tool.

    Each action exits 0 on success, 1 on user-visible error. Errors print
    to stderr; success output goes to stdout.
    """
    action = args.action
    if action == "list":
        status = args.status or "Pending"
        rows = db.list_contracts(status=status, scope=args.scope)
        if not rows:
            scope_str = f" scope={args.scope}" if args.scope else ""
            print(f"No contracts in status={status}{scope_str}")
            return
        for i, r in enumerate(rows):
            if i:
                print()
            _print_contract_row(r)
        return

    if action == "show":
        if not args.id:
            print("contracts show: --id is required", file=sys.stderr)
            sys.exit(1)
        row = db.get_contract(args.id)
        if row is None:
            print(f"contracts show: {args.id} not found", file=sys.stderr)
            sys.exit(1)
        _print_contract_row(row)
        return

    if action == "approve":
        if not args.id:
            print("contracts approve: --id is required", file=sys.stderr)
            sys.exit(1)
        if not db.approve_contract(args.id):
            print(f"contracts approve: {args.id} not found or not Pending", file=sys.stderr)
            sys.exit(1)
        print(f"approved {args.id}")
        return

    if action == "reject":
        if not args.id:
            print("contracts reject: --id is required", file=sys.stderr)
            sys.exit(1)
        if not db.reject_contract(args.id):
            print(f"contracts reject: {args.id} not found or not Pending", file=sys.stderr)
            sys.exit(1)
        print(f"rejected {args.id}")
        return

    if action == "retract":
        if not args.id or not args.reason:
            print("contracts retract: --id and --reason are required", file=sys.stderr)
            sys.exit(1)
        try:
            ok = db.retract_contract(args.id, reason=args.reason)
        except ValueError as e:
            print(f"contracts retract: {e}", file=sys.stderr)
            sys.exit(1)
        if not ok:
            print(f"contracts retract: {args.id} not found or not Active", file=sys.stderr)
            sys.exit(1)
        print(f"retracted {args.id}")
        return

    if action == "supersede":
        if not args.id or not args.body:
            print("contracts supersede: --id and --body are required", file=sys.stderr)
            sys.exit(1)
        new_id = db.supersede_contract(
            args.id,
            new_body=args.body,
            byline_session_id=args.byline_session_id,
            byline_model=args.byline_model,
        )
        if new_id is None:
            row = db.get_contract(args.id)
            if row is None:
                print(f"contracts supersede: {args.id} not found", file=sys.stderr)
            else:
                print("contracts supersede: target not Active or new body is a duplicate", file=sys.stderr)
            sys.exit(1)
        print(f"superseded {args.id} -> {new_id}")
        return

    print(f"contracts: unknown action {action!r}", file=sys.stderr)
    sys.exit(1)


def cmd_codex(args):
    """Codex setup, diagnostics, and launcher helpers."""
    action = args.codex_action
    if action == "doctor":
        checks = codex_support.collect_doctor_checks(
            codex_cmd=args.codex_cmd,
            codex_home=args.codex_home,
            cwd=args.cwd or os.getcwd(),
        )
        print(codex_support.format_checks(checks))
        return
    if action == "install-hooks":
        if args.write:
            path = codex_support.write_hook_config(
                args.scope,
                codex_home=args.codex_home,
                cwd=args.cwd or os.getcwd(),
            )
            print(f"Wrote Codex hook config to {path}")
        else:
            print(codex_support.print_hook_dry_run(
                args.scope,
                codex_home=args.codex_home,
                cwd=args.cwd or os.getcwd(),
            ))
        return
    if action == "install-mcp":
        if args.write:
            import subprocess
            proc = subprocess.run(
                codex_support.mcp_add_command(codex_cmd=args.codex_cmd),
                check=False,
            )
            sys.exit(proc.returncode)
        print(codex_support.print_mcp_dry_run(codex_cmd=args.codex_cmd))
        return
    if action == "start":
        prompt = " ".join(args.prompt or [])
        if args.print_context:
            print(codex_support.build_launcher_prompt(prompt, cwd=args.cwd or os.getcwd()))
            return
        code = codex_support.launch_codex_with_context(
            prompt,
            codex_cmd=args.codex_cmd,
            cwd=args.cwd or os.getcwd(),
            extra_args=args.codex_arg or [],
        )
        sys.exit(code)
    print(f"codex: unknown action {action!r}", file=sys.stderr)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        prog="lcc",
        description="Lossless Context Commands for Claude Code",
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # summarise
    p_sum = sub.add_parser("summarise", help="Run compaction (summarisation)")
    p_sum.add_argument("--run", action="store_true", help="Execute compaction now")
    p_sum.add_argument("--session", help="Limit to a specific session ID")
    p_sum.set_defaults(func=cmd_summarise)

    # grep
    p_grep = sub.add_parser("grep", help="Full-text search")
    p_grep.add_argument("query", help="Search query")
    p_grep.add_argument("--limit", type=int, default=20)
    p_grep.set_defaults(func=cmd_grep)

    # expand
    p_expand = sub.add_parser("expand", help="Expand a summary node")
    p_expand.add_argument(
        "summary_id",
        nargs="?",
        default=None,
        help="Summary ID (e.g. sum_abc123); omit when using --file or --span-id",
    )
    p_expand.add_argument(
        "--file",
        default=None,
        help="File path to expand; lists recent summaries that mention it",
    )
    p_expand.add_argument(
        "--span-id",
        dest="span_id",
        default=None,
        help="Message ID; walks the parent_message_id chain (v1.2 span mode)",
    )
    p_expand.add_argument(
        "--limit", type=int, default=3, help="Max summaries when using --file"
    )
    p_expand.add_argument("--full", action="store_true", help="Show full content")
    p_expand.set_defaults(func=cmd_expand)

    # context
    p_ctx = sub.add_parser("context", help="Print the v1.2 SessionStart bundle")
    p_ctx.set_defaults(func=cmd_context)

    # sessions
    p_ses = sub.add_parser("sessions", help="List sessions")
    p_ses.add_argument("--limit", type=int, default=20)
    p_ses.set_defaults(func=cmd_sessions)

    # handoff
    p_ho = sub.add_parser("handoff", help="Show/generate handoff")
    p_ho.add_argument("--session", help="Session ID")
    p_ho.add_argument("--generate", action="store_true", help="Generate handoff")
    p_ho.set_defaults(func=cmd_handoff)

    # status
    p_st = sub.add_parser("status", help="Vault statistics")
    p_st.set_defaults(func=cmd_status)

    # reindex
    p_reindex = sub.add_parser("reindex", help="Embed un-indexed messages for hybrid search")
    p_reindex.add_argument("--embeddings", action="store_true", help="Embed all un-indexed messages")
    p_reindex.add_argument("--model", help="Override embeddingModel for this run")
    p_reindex.add_argument("--force", action="store_true", help="Re-embed all messages even if already indexed")
    p_reindex.set_defaults(func=cmd_reindex)

    # dream
    p_dream = sub.add_parser("dream", help="Run dream cycle — extract patterns and consolidate DAG")
    p_dream.add_argument("--run", action="store_true", help="Execute dream now")
    p_dream.add_argument("--project", help="Scope to specific working directory")
    p_dream.add_argument("--global", action="store_true", dest="global_scope", help="Run global cross-project dream")
    p_dream.set_defaults(func=cmd_dream)

    # contracts (v1.2 U7)
    p_contracts = sub.add_parser(
        "contracts",
        help="Behavior-contract registry: list / show / approve / reject / retract / supersede",
    )
    p_contracts.add_argument(
        "action",
        choices=["list", "show", "approve", "reject", "retract", "supersede"],
        help="What to do",
    )
    p_contracts.add_argument("--id", help="Contract id (con_...)")
    p_contracts.add_argument(
        "--status",
        choices=["Pending", "Active", "Retracted", "Rejected"],
        help="Filter for list (default: Pending)",
    )
    p_contracts.add_argument("--scope", help="Filter for list")
    p_contracts.add_argument("--reason", help="Required for retract")
    p_contracts.add_argument("--body", help="Required for supersede")
    p_contracts.add_argument("--byline-session-id", dest="byline_session_id")
    p_contracts.add_argument("--byline-model", dest="byline_model")
    p_contracts.set_defaults(func=cmd_contracts)

    # codex
    p_codex = sub.add_parser("codex", help="Codex setup, diagnostics, and launcher helpers")
    codex_sub = p_codex.add_subparsers(dest="codex_action", help="Codex actions")

    p_codex_doctor = codex_sub.add_parser("doctor", help="Check Codex readiness")
    p_codex_doctor.add_argument("--codex-cmd", default="codex")
    p_codex_doctor.add_argument("--codex-home")
    p_codex_doctor.add_argument("--cwd")
    p_codex_doctor.set_defaults(func=cmd_codex)

    p_codex_hooks = codex_sub.add_parser("install-hooks", help="Print or write Codex hook config")
    p_codex_hooks.add_argument("--scope", choices=["project", "user"], default="project")
    p_codex_hooks.add_argument("--write", action="store_true")
    p_codex_hooks.add_argument("--codex-home")
    p_codex_hooks.add_argument("--cwd")
    p_codex_hooks.set_defaults(func=cmd_codex)

    p_codex_mcp = codex_sub.add_parser("install-mcp", help="Print or run Codex MCP registration")
    p_codex_mcp.add_argument("--write", action="store_true")
    p_codex_mcp.add_argument("--codex-cmd", default="codex")
    p_codex_mcp.set_defaults(func=cmd_codex)

    p_codex_start = codex_sub.add_parser("start", help="Launch Codex with Lossless-Code context")
    p_codex_start.add_argument("--print-context", action="store_true")
    p_codex_start.add_argument("--codex-cmd", default="codex")
    p_codex_start.add_argument("--cwd")
    p_codex_start.add_argument("--codex-arg", action="append", default=[])
    p_codex_start.add_argument("prompt", nargs=argparse.REMAINDER)
    p_codex_start.set_defaults(func=cmd_codex)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    if args.command == "codex" and not args.codex_action:
        p_codex.print_help()
        return

    if args.command == "summarise" and not args.run:
        print("Use --run to execute compaction. Example: lcc summarise --run")
        return

    if args.command == "dream" and not args.run:
        print("Use --run to execute dream cycle. Example: lcc dream --run")
        return

    args.func(args)


if __name__ == "__main__":
    main()
