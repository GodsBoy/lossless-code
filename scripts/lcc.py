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
import dream as dream_mod
import embed as embed_mod
import inject_context
import summarise as summarise_mod


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
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(m["timestamp"]))
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
                ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(m["timestamp"]))
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


def cmd_context(args):
    """Surface top N relevant DAG nodes for a query."""
    context = inject_context.build_context(
        query=args.query,
        limit=args.limit,
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

        # Build handoff text
        cfg = db.load_config()
        text = summarise_mod.format_messages_for_summary(messages)
        prompt_text = (
            "Generate a concise handoff summary for the next coding session. "
            "Include: what was worked on, key decisions made, current state, "
            "what needs to happen next. Be specific with file paths and commands.\n\n"
            f"{text}"
        )
        handoff_text = summarise_mod.call_summary_model(prompt_text, cfg)
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
    """Show vault statistics."""
    d = db.get_db()
    msg_count = d.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    sum_count = d.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
    ses_count = d.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    unsummarised = d.execute("SELECT COUNT(*) FROM messages WHERE summarised = 0").fetchone()[0]
    max_depth = d.execute("SELECT COALESCE(MAX(depth), 0) FROM summaries").fetchone()[0]

    vault_size = os.path.getsize(db.VAULT_DB) if db.VAULT_DB.exists() else 0
    vault_mb = vault_size / (1024 * 1024)

    # Dream stats
    dream_count = d.execute("SELECT COUNT(*) FROM dream_log").fetchone()[0]
    last_dream_row = d.execute("SELECT dreamed_at FROM dream_log ORDER BY dreamed_at DESC LIMIT 1").fetchone()
    last_dream = time.strftime("%Y-%m-%d %H:%M", time.localtime(last_dream_row[0])) if last_dream_row else "never"
    consolidated = d.execute("SELECT COUNT(*) FROM summaries WHERE consolidated = 1").fetchone()[0]

    cfg = db.load_config()
    # Vector search status
    embed_enabled = cfg.get("embeddingEnabled", False)
    embed_model = cfg.get("embeddingModel", "BAAI/bge-small-en-v1.5")
    if embed_enabled:
        provider = embed_mod.detect_provider(cfg)
        cov = db.get_embedding_model_coverage(embed_model)
        if provider and provider != "numpy":
            vec_status = f"active ({provider}, {embed_model})"
        else:
            vec_status = f"inactive (no provider available — pip install lossless-code[embed])"
        embed_line = (
            f"  Embeddings:    {cov['embedded']:,} / {cov['total']:,} messages indexed"
            f"  ({cov['pending']:,} pending)"
        )
    else:
        vec_status = "inactive (embeddingEnabled: false)"
        embed_line = None

    print(f"lossless-code vault status")
    print(f"  Vault:         {db.VAULT_DB} ({vault_mb:.2f} MB)")
    print(f"  Sessions:      {ses_count}")
    print(f"  Messages:      {msg_count} ({unsummarised} unsummarised)")
    print(f"  Summaries:     {sum_count} (max depth: {max_depth}, {consolidated} consolidated)")
    print(f"  Dreams:        {dream_count} (last: {last_dream})")
    print(f"  Vector search: {vec_status}")
    if embed_line:
        print(embed_line)


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
    scope = "global" if args.global_scope else "project"
    working_dir = args.project or os.getcwd()
    cfg = db.load_config()
    report = dream_mod.run_dream(scope, working_dir, cfg)
    print(report)


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
    p_expand.add_argument("summary_id", help="Summary ID (e.g. sum_abc123)")
    p_expand.add_argument("--full", action="store_true", help="Show full content")
    p_expand.set_defaults(func=cmd_expand)

    # context
    p_ctx = sub.add_parser("context", help="Surface relevant context")
    p_ctx.add_argument("query", nargs="?", default="", help="Query")
    p_ctx.add_argument("--limit", type=int, default=5)
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

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
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
