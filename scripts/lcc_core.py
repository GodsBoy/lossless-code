"""Shared status-collection helper used by the CLI and MCP surfaces.

Closes the historical CLI/MCP-divergence pattern documented in
phase2-hybrid-search-anti-patterns-and-review-findings.md: lcc.py
cmd_status and mcp/server.py _do_status used to assemble their own
near-identical strings, drifting field-by-field over releases. This
module is the single source of truth; each surface formats the dict
differently for human vs agent presentation.
"""

import os
import time
from typing import Optional


def collect_status_dict(working_dir: Optional[str] = None) -> dict:
    """Gather every status field both surfaces need into one dict.

    working_dir is unused today but accepted so future fields (e.g.
    bundle_token_count for the cwd's actual bundle) can land without
    a signature change.
    """
    import db
    import summarise as summarise_mod

    d = db.get_db()
    cfg = db.load_config()

    msg_count = d.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    sum_count = d.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
    ses_count = d.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    unsummarised = d.execute(
        "SELECT COUNT(*) FROM messages WHERE summarised = 0"
    ).fetchone()[0]
    max_depth_row = d.execute(
        "SELECT COALESCE(MAX(depth), 0) FROM summaries"
    ).fetchone()
    max_depth = max_depth_row[0] if max_depth_row else 0

    vault_path = str(db.VAULT_DB)
    vault_bytes = os.path.getsize(vault_path) if db.VAULT_DB.exists() else 0

    # Dream stats (existing fields plus v1.2 lastDreamMode)
    dream_count = d.execute("SELECT COUNT(*) FROM dream_log").fetchone()[0]
    last_dream_row = d.execute(
        "SELECT dreamed_at, mode FROM dream_log "
        "ORDER BY dreamed_at DESC LIMIT 1"
    ).fetchone()
    if last_dream_row:
        last_dream_at = last_dream_row[0]
        last_dream_mode = last_dream_row[1]  # may be NULL on legacy rows
    else:
        last_dream_at = None
        last_dream_mode = None
    consolidated = d.execute(
        "SELECT COUNT(*) FROM summaries WHERE consolidated = 1"
    ).fetchone()[0]

    # v1.2 contract counts (U7 surface, U10 bundle reads these for the
    # contracts slot).
    contracts_pending = d.execute(
        "SELECT COUNT(*) FROM contracts WHERE status = 'Pending'"
    ).fetchone()[0]
    contracts_active = d.execute(
        "SELECT COUNT(*) FROM contracts WHERE status = 'Active'"
    ).fetchone()[0]
    contracts_retracted = d.execute(
        "SELECT COUNT(*) FROM contracts WHERE status = 'Retracted'"
    ).fetchone()[0]
    contracts_rejected = d.execute(
        "SELECT COUNT(*) FROM contracts WHERE status = 'Rejected'"
    ).fetchone()[0]

    # v1.2 decision count (summaries.kind='decision' rows the bundle
    # reads for the decisions slot).
    decisions_count = d.execute(
        "SELECT COUNT(*) FROM summaries WHERE kind = 'decision'"
    ).fetchone()[0]

    # Provider info (R5 in v1.1)
    pinfo = summarise_mod.get_provider_info()

    # Embedding info
    embed_enabled = bool(cfg.get("embeddingEnabled", False))
    embed_model = cfg.get("embeddingModel", "BAAI/bge-small-en-v1.5")
    embed_coverage: Optional[dict] = None
    embed_provider: Optional[str] = None
    if embed_enabled:
        try:
            import embed as embed_mod
            embed_provider = embed_mod.detect_provider(cfg)
            embed_coverage = db.get_embedding_model_coverage(embed_model)
        except Exception:
            embed_coverage = None
            embed_provider = None

    # File-context fingerprint coverage (gated on config)
    file_context_enabled = bool(cfg.get("fileContextEnabled", False))
    file_tagged = 0
    distinct_files = 0
    fingerprint_cache_count = 0
    if file_context_enabled:
        file_tagged = d.execute(
            "SELECT COUNT(*) FROM messages WHERE file_path IS NOT NULL"
        ).fetchone()[0]
        distinct_files = d.execute(
            "SELECT COUNT(DISTINCT file_path) FROM messages "
            "WHERE file_path IS NOT NULL"
        ).fetchone()[0]
        try:
            import file_context as fc
            fingerprint_cache_count = fc.cache_size()
        except Exception:
            fingerprint_cache_count = 0

    return {
        # --- Vault ---
        "vault_path": vault_path,
        "vault_bytes": vault_bytes,
        "session_count": ses_count,
        "message_count": msg_count,
        "summary_count": sum_count,
        "unsummarised_count": unsummarised,
        "max_summary_depth": max_depth,
        "consolidated_count": consolidated,
        # --- Dream cycle ---
        "dream_count": dream_count,
        "last_dream_at": last_dream_at,
        "last_dream_mode": last_dream_mode,
        # --- v1.2 contracts (U5-U7) ---
        "contracts_pending": contracts_pending,
        "contracts_active": contracts_active,
        "contracts_retracted": contracts_retracted,
        "contracts_rejected": contracts_rejected,
        # --- v1.2 decisions ---
        "decisions_count": decisions_count,
        # --- v1.2 bundle (U10) ---
        "bundle_enabled": bool(cfg.get("bundleEnabled", True)),
        "bundle_token_budget": int(cfg.get("bundleTokenBudget", 1000)),
        # --- Embedding ---
        "embedding_enabled": embed_enabled,
        "embedding_model": embed_model,
        "embedding_provider": embed_provider,
        "embedding_coverage": embed_coverage,
        # --- Provider ---
        "provider": pinfo.get("provider"),
        "model": pinfo.get("model"),
        "provider_auto_detected": bool(pinfo.get("auto_detected")),
        "provider_last_error": pinfo.get("last_error"),
        "provider_last_error_time": pinfo.get("last_error_time"),
        # --- File context ---
        "file_context_enabled": file_context_enabled,
        "file_tagged_messages": file_tagged,
        "distinct_files": distinct_files,
        "fingerprint_cache_count": fingerprint_cache_count,
    }


def format_status_human(s: dict) -> str:
    """Render the status dict as a multi-line string for human consumption.

    Used by both cmd_status (CLI) and _do_status (MCP). Keeping the
    formatter here means the two surfaces never drift on the wording or
    field set.
    """
    vault_mb = s["vault_bytes"] / (1024 * 1024)

    lines = [
        "lossless-code vault status",
        f"  Vault:         {s['vault_path']} ({vault_mb:.2f} MB)",
        f"  Sessions:      {s['session_count']}",
        f"  Messages:      {s['message_count']} ({s['unsummarised_count']} unsummarised)",
        f"  Summaries:     {s['summary_count']} (max depth: {s['max_summary_depth']}, "
        f"{s['consolidated_count']} consolidated)",
    ]

    # Dream cycle line
    last_dream_str = (
        time.strftime("%Y-%m-%d %H:%M", time.localtime(s["last_dream_at"]))
        if s["last_dream_at"] else "never"
    )
    mode_str = f", mode={s['last_dream_mode']}" if s["last_dream_mode"] else ""
    lines.append(f"  Dreams:        {s['dream_count']} (last: {last_dream_str}{mode_str})")

    # v1.2 contracts
    lines.append(
        f"  Contracts:     {s['contracts_active']} active, "
        f"{s['contracts_pending']} pending, "
        f"{s['contracts_retracted']} retracted, "
        f"{s['contracts_rejected']} rejected"
    )

    # v1.2 decisions
    lines.append(f"  Decisions:     {s['decisions_count']} (typed summary nodes)")

    # v1.2 bundle
    bundle_state = "enabled" if s["bundle_enabled"] else "DISABLED (bundleEnabled=false)"
    lines.append(
        f"  Bundle:        {bundle_state}, budget {s['bundle_token_budget']} tokens"
    )

    # Vector search
    if s["embedding_enabled"]:
        prov = s["embedding_provider"]
        if prov and prov != "numpy":
            vec_status = f"active ({prov}, {s['embedding_model']})"
        else:
            vec_status = "inactive (no provider available; pip install lossless-code[embed])"
        lines.append(f"  Vector search: {vec_status}")
        cov = s["embedding_coverage"]
        if cov:
            lines.append(
                f"  Embeddings:    {cov['embedded']:,} / {cov['total']:,} messages indexed"
                f"  ({cov['pending']:,} pending)"
            )
    else:
        lines.append("  Vector search: inactive (embeddingEnabled: false)")

    # Provider info
    p_name = s.get("provider") or "none"
    p_model = s.get("model") or "none"
    p_suffix = " via auto-detect" if s.get("provider_auto_detected") else ""
    p_err = s.get("provider_last_error")
    if p_err:
        err_time = s.get("provider_last_error_time")
        if err_time:
            ago = int(time.time() - err_time)
            err_ago = f"{ago // 60}m ago" if ago < 3600 else f"{ago // 3600}h ago"
            err_str = f"{p_err} {err_ago}"
        else:
            err_str = str(p_err)
    else:
        err_str = "none"
    lines.append(f"  Provider:      {p_name} ({p_model}){p_suffix}")
    lines.append(f"               Last error: {err_str}")

    # File-context fingerprint
    if s["file_context_enabled"]:
        lines.append(
            f"  Fingerprint:   {s['file_tagged_messages']} tagged messages across "
            f"{s['distinct_files']} files ({s['fingerprint_cache_count']} cached)"
        )

    return "\n".join(lines)


__all__ = ["collect_status_dict", "format_status_human"]
