---
status: pending
priority: p1
issue_id: "003"
tags: [code-review, architecture, agent-native, mcp]
dependencies: []
---

# MCP `lcc_grep` Uses FTS5-Only — Bypasses Hybrid Search

## Problem Statement

The MCP tool `lcc_grep` calls `db.search_all()` (FTS5-only) regardless of whether `embeddingEnabled=true`. The CLI `lcc grep` calls `embed.hybrid_search()` which does RRF-fused vector+FTS5 when embeddings are enabled. Agents using MCP get worse search results than users running the CLI — with no indication of the degradation and no `[hybrid]` tag in results. This defeats the primary value proposition of Phase 2 for agent-native use.

## Findings

**Agent:** agent-native-reviewer (P1)

- `mcp/server.py:187` (approx) — `_do_grep` calls `db.search_all(query, limit)` — pure FTS5
- `scripts/lcc.py:32` — `cmd_grep` calls `embed_mod.hybrid_search(args.query, cfg, limit=args.limit)` — hybrid when enabled
- `embed.hybrid_search()` already degrades gracefully to FTS5 when embeddings are unavailable — zero risk of new failure modes

## Proposed Solutions

### Option A — Wire `_do_grep` to `embed.hybrid_search` (Recommended)

```python
# mcp/server.py _do_grep
import embed as embed_mod
cfg = db.load_config()
results = embed_mod.hybrid_search(query, cfg, limit=limit)
```

The function already returns `{"messages": [...], "summaries": [...]}` in the same shape as `db.search_all()`. When `embeddingEnabled=false` it returns FTS5 results unchanged.

- Pros: Full parity with CLI; zero new failure modes due to graceful fallback; cfg load is cheap
- Cons: Adds `embed` import to mcp/server.py (new dependency, but embed is co-located)
- Effort: Small (2–4 lines)
- Risk: Very low

### Option B — Add `hybrid` parameter to `lcc_grep` tool

Pass `hybrid: bool` from the tool call and branch at server level.

- Pros: Explicit opt-in, no silent behaviour change for existing consumers
- Cons: Adds tool complexity; agent must know to pass `hybrid=True`; default `hybrid=False` perpetuates the gap
- Effort: Medium
- Risk: Low

## Recommended Action

_Option A — wire `_do_grep` to `embed.hybrid_search` unconditionally. The function already handles the disabled case correctly. No new failure modes._

## Technical Details

**Affected files:** `scripts/mcp/server.py` (or equivalent MCP handler)
**Database changes:** None

## Acceptance Criteria

- [ ] `lcc_grep` MCP tool calls `embed.hybrid_search()` not `db.search_all()`
- [ ] When `embeddingEnabled=false`, MCP results are identical to current (FTS5 only)
- [ ] When `embeddingEnabled=true`, MCP results include vector-ranked results
- [ ] MCP response includes `hybrid: true` field when hybrid search ran
- [ ] 148+ tests pass

## Work Log

- 2026-03-29 — Identified by agent-native-reviewer

## Resources

- MCP handler file (check `mcp/server.py` or `scripts/lcc.py` MCP section)
- `scripts/embed.py:291–365` (`hybrid_search` implementation)
