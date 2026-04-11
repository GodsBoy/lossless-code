---
status: superseded
priority: p3
issue_id: "017"
tags: [code-review, architecture, search]
dependencies: ["003"]
---

# Context Injection Path Uses FTS5-Only — Does Not Benefit from Hybrid Search

## Problem Statement

`inject_context.py` calls `db.search_all()` which is pure FTS5. Even when `embeddingEnabled=true` and the vault is fully indexed, the context injection path (used by `lcc_context` MCP tool and the `UserPromptSubmit` hook) does not use hybrid search. The plan document specified updating `db.search_all()` to call hybrid search when active — this was not done. Phase 2's search quality improvement only applies to `lcc grep` / `lcc_grep` MCP, not to automatic context retrieval.

## Findings

**Agent:** learnings-researcher (conflict flag: "plan said to wire `db.search_all()` to hybrid; this did not happen")

- `scripts/inject_context.py` — calls `db.search_all(query, limit)` — FTS5 only
- Plan `docs/plans/2026-03-29-001-feat-semantic-search-hybrid-plan.md` — "Update `db.search_all()` to call `search_hybrid_rrf()` when vector search is active"
- The ADR documents the decision to keep `search_vector` in `embed.py` — but the plan's intent of wiring context injection to hybrid search was not carried through

## Proposed Solutions

### Option A — Wire `inject_context.py` to `embed.hybrid_search` (Recommended)

```python
# inject_context.py
import embed as embed_mod
cfg = db.load_config()
results = embed_mod.hybrid_search(query, cfg, limit=limit)
```

- Pros: Context injection benefits from semantic search; consistent with CLI behaviour
- Cons: Adds embed dependency to inject_context.py
- Effort: Small

### Option B — Accept FTS5-only context injection as intentional

Document the decision in the ADR. Keep context injection fast/simple; reserve hybrid search for explicit user queries.

- Pros: Context injection stays simple and fast; no model load overhead on every UserPromptSubmit
- Cons: Semantic search doesn't improve the core context-building flow
- Effort: Zero (just document)

## Recommended Action

_Confirm intent with Dewaldt. Option B (leave FTS5) may be correct if context injection should remain fast. Option A if context quality is the priority._

## Technical Details

**Affected files:** `scripts/inject_context.py`

## Acceptance Criteria

- [ ] Decision documented: context injection uses hybrid or FTS5-only (with rationale)
- [ ] If hybrid: `inject_context.py` calls `embed.hybrid_search`; tests cover both cases

## Work Log

- 2026-03-29 — Identified by learnings-researcher as plan deviation
- 2026-04-11 — Superseded by BM25 prompt-aware eviction (docs/plans/2026-04-11-001-feat-bm25-prompt-aware-eviction-plan.md). Context injection now uses FTS5 BM25 ranking with budget-aware packing.
