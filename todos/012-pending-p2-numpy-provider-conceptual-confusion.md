---
status: pending
priority: p2
issue_id: "012"
tags: [code-review, architecture, quality]
dependencies: []
---

# `detect_provider` Conflates Generation Capability with Search Capability

## Problem Statement

`detect_provider()` returns `"numpy"` to mean "can search existing BLOBs but cannot generate new embeddings." This conflates two distinct concepts: embedding _generation_ (requires fastembed/openai/anthropic) vs. embedding _search_ (requires only numpy). The `"numpy"` provider string is architecturally misleading and causes unnecessary guards throughout the codebase (`if provider is None or provider == "numpy": return 0`). `lcc status` reporting `"numpy"` as the provider implies the system is functional when it is degraded.

## Findings

**Agent:** architecture-strategist (P2)

- `embed.py:46–50` — `"numpy"` returned as provider when only numpy is available
- `embed.py:197` — `if provider is None or provider == "numpy": return 0` in `embed_messages_batch`
- `embed.py:239` — same check in `reindex_vault`
- `embed.py:325` — same check in `hybrid_search`
- `lcc.py:202–227` — status output shows `"numpy"` as active provider

## Proposed Solutions

### Option A — Separate `can_generate` and `can_search` concepts (Recommended)

```python
def detect_generation_provider(cfg: dict) -> str | None:
    """Returns "fastembed" | "openai" | "anthropic" | None.
    numpy is NOT a generation provider — it cannot produce new embeddings."""
    ...

def can_vector_search(cfg: dict) -> bool:
    """Returns True if numpy is available for BLOB cosine search."""
    try:
        import numpy  # noqa: F401
        return True
    except ImportError:
        return False
```

- Pros: Clear API contract; eliminates `provider == "numpy"` guards; `lcc status` shows accurate state
- Cons: Two functions instead of one; callers need updating
- Effort: Medium

### Option B — Keep single function, rename return value

Return `"numpy_search_only"` instead of `"numpy"` to make the limitation explicit.

- Pros: Minimal change
- Cons: Still conflates generation and search in one function; string comparison still required everywhere
- Effort: Small

## Recommended Action

_Option A — separate functions. The generation/search distinction is a genuine architectural boundary that will matter more as the `vectorBackend` config key gets implemented._

## Technical Details

**Affected files:** `scripts/embed.py`, `scripts/lcc.py`

## Acceptance Criteria

- [ ] `detect_provider` (or renamed equivalent) only returns providers that can generate embeddings
- [ ] numpy-only case has a separate, clearly named function
- [ ] `lcc status` correctly reports "inactive (no generation provider)" vs "active"
- [ ] No `provider == "numpy"` guards remaining in codebase

## Work Log

- 2026-03-29 — Identified by architecture-strategist
