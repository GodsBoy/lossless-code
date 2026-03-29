---
status: pending
priority: p1
issue_id: "005"
tags: [code-review, agent-native, mcp, architecture]
dependencies: []
---

# MCP `lcc_status` Omits All Phase 2 Embedding + Dream Fields

## Problem Statement

`lcc_status` MCP tool returns a hardcoded set of pre-Phase-2 fields. It omits: embedding enabled/disabled state, active provider, embedding model, message coverage (embedded vs pending), dream stats (count, last run, consolidated), and vector search health. Agents cannot determine whether hybrid search is active, how much of the vault is indexed, or whether a reindex is warranted. The status tool predates Phase 2 entirely.

## Findings

**Agents:** agent-native-reviewer (P1), learnings-researcher (supplementary)

- MCP `_do_status` returns ~5 hardcoded fields from Phase 1
- `scripts/lcc.py:cmd_status:182–227` outputs: vault path, session count, message count, summary stats, dream stats, vector search active/inactive, provider, model, coverage (embedded/total/pending)
- `db.get_embedding_model_coverage(model)` and `embed.detect_provider(cfg)` are already implemented and available

## Proposed Solutions

### Option A — Mirror `cmd_status` output in `_do_status` (Recommended)

```python
def _do_status():
    cfg = db.load_config()
    model = cfg.get("embeddingModel", "BAAI/bge-small-en-v1.5")
    embed_enabled = cfg.get("embeddingEnabled", False)
    provider = embed_mod.detect_provider(cfg) if embed_enabled else None
    coverage = db.get_embedding_model_coverage(model) if embed_enabled else {}
    dream_count = db.execute("SELECT COUNT(*) FROM dream_log").fetchone()[0]
    # ... return all fields
    return {
        # existing fields...
        "embeddingEnabled": embed_enabled,
        "embeddingProvider": provider,
        "embeddingModel": model if embed_enabled else None,
        "embeddedMessages": coverage.get("embedded", 0),
        "pendingMessages": coverage.get("pending", 0),
        "dreamCount": dream_count,
        # ...
    }
```

- Pros: Full parity with CLI; enables agents to self-diagnose and guide users
- Cons: Slightly more fields in response — backward compatible (additive only)
- Effort: Small
- Risk: Very low (all DB calls are existing, read-only)

## Recommended Action

_Option A — mirror `cmd_status` output. Additive change only, no backward compat risk._

## Technical Details

**Affected files:** MCP server handler
**Database changes:** None

## Acceptance Criteria

- [ ] `lcc_status` returns `embeddingEnabled`, `embeddingProvider`, `embeddingModel` fields
- [ ] Returns `embeddedMessages`, `totalMessages`, `pendingMessages` coverage fields
- [ ] Returns dream stats: `dreamCount`, `lastDreamTime`
- [ ] All existing fields still present (backward compatible)

## Work Log

- 2026-03-29 — Identified by agent-native-reviewer

## Resources

- `scripts/lcc.py:182–227` (reference implementation)
- `scripts/db.py:get_embedding_model_coverage`
