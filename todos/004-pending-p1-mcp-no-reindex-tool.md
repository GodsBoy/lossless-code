---
status: pending
priority: p1
issue_id: "004"
tags: [code-review, agent-native, mcp, architecture]
dependencies: ["003"]
---

# No MCP Tool for `lcc reindex --embeddings`

## Problem Statement

There is no MCP equivalent for `lcc reindex --embeddings`. Agents cannot backfill embeddings for historical messages, cannot recover after a model change (which leaves the index stale and hybrid search silently falling back to FTS5), and cannot rebuild a corrupted index. In environments where the Stop hook does not fire reliably (sub-agents, CI, custom harnesses), messages accumulate without embeddings indefinitely.

## Findings

**Agents:** agent-native-reviewer (P1), learnings-researcher (P2)

- `scripts/lcc.py:cmd_reindex` — calls `embed.reindex_vault(cfg, force=args.force, model_override=args.model)`
- No MCP tool wraps this function
- `embed.hybrid_search` at lines 314–322 detects model mismatch and falls back to FTS5 with a stderr warning — recovery requires `lcc reindex --embeddings --force` which has no MCP path

## Proposed Solutions

### Option A — Add `lcc_reindex` MCP tool (Recommended)

```python
@tool("lcc_reindex")
def lcc_reindex_tool(embeddings: bool = False, force: bool = False, model: str = "") -> dict:
    """Reindex vault. Pass embeddings=True to rebuild vector index."""
    if embeddings:
        cfg = db.load_config()
        if model:
            cfg = {**cfg, "embeddingModel": model}
        count = embed_mod.reindex_vault(cfg, force=force, model_override=model or None)
        return {"indexed": count, "force": force, "model": cfg["embeddingModel"]}
    # existing FTS5 reindex logic
```

- Pros: Full recovery capability for agents; unblocks background embedding in hook-less envs
- Cons: reindex_vault prints progress to stdout — should be suppressed or captured in MCP context
- Effort: Small
- Risk: Low (reindex_vault is already safe and idempotent)

### Option B — Expose only force-reindex, not model override

Simpler tool with just `force: bool`.

- Pros: Smaller surface area
- Cons: Agent cannot recover from model-change scenario without model override
- Effort: Trivial

## Recommended Action

_Option A — full `lcc_reindex` tool. Wrap stdout during reindex (capture or suppress print output) and return a structured result._

## Technical Details

**Affected files:** `scripts/mcp/server.py` (or MCP handler)
**Database changes:** None

## Acceptance Criteria

- [ ] `lcc_reindex` MCP tool exists with `embeddings: bool`, `force: bool`, `model: str` parameters
- [ ] Returns count of newly indexed messages
- [ ] Works correctly when called from a non-hook environment
- [ ] reindex progress does not bleed into MCP stdout response

## Work Log

- 2026-03-29 — Identified by agent-native-reviewer

## Resources

- `scripts/embed.py:228–284` (`reindex_vault` implementation)
- `scripts/lcc.py:cmd_reindex`
