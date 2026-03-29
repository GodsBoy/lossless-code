---
status: pending
priority: p1
issue_id: "006"
tags: [code-review, performance, python]
dependencies: []
---

# fastembed `TextEmbedding` Instantiated on Every Call — No Caching

## Problem Statement

`_fastembed_embed` constructs a new `TextEmbedding(model_name=model_name)` on every invocation. fastembed model cold load takes 500ms–2s. This fires on every `lcc grep` (interactive search) and on every 32-message batch during `reindex_vault`. For a 2,649-message vault, reindexing wastes ~62 seconds on model loads before any actual inference.

## Findings

**Agent:** performance-oracle (P1)

- `embed.py:101` — `embedder = TextEmbedding(model_name=model_name)` — no caching
- Called by `embed_texts()` → called by `hybrid_search()` on every `lcc grep`
- Called by `embed_texts()` → called by `reindex_vault()` once per 32-message batch
- At 2,649 messages: 83 batches × ~750ms model load = ~62s wasted on loads
- At 10,000 messages: 313 batches × ~750ms = ~235s wasted

**Scale threshold:** Even at current scale, every interactive `lcc grep` has 500ms–2s latency due to model reload before FTS5+vector fusion begins.

## Proposed Solutions

### Option A — Module-level dict cache keyed by model name (Recommended)

```python
_embedder_cache: dict[str, "TextEmbedding"] = {}

def _fastembed_embed(texts: list[str], model_name: str) -> list[Optional[list[float]]]:
    try:
        from fastembed import TextEmbedding
        if model_name not in _embedder_cache:
            _embedder_cache[model_name] = TextEmbedding(model_name=model_name)
        embedder = _embedder_cache[model_name]
        results = []
        for vec in embedder.embed(texts):
            results.append([float(v) for v in vec])
        return results
    except Exception:
        return [None] * len(texts)
```

- Pros: Eliminates all repeat loads in a process; trivial change; safe (fastembed models are thread-safe read-only after load)
- Cons: Model stays in memory for process lifetime — ~200MB for bge-small. Acceptable for a CLI tool.
- Effort: Trivial (5 lines)
- Risk: Very low

### Option B — LRU cache with size limit

```python
from functools import lru_cache

@lru_cache(maxsize=3)
def _get_embedder(model_name: str):
    from fastembed import TextEmbedding
    return TextEmbedding(model_name=model_name)
```

- Pros: Bounded memory; standard library; evicts unused models
- Cons: `lru_cache` on module-level function is slightly less obvious than a dict
- Effort: Trivial

## Recommended Action

_Option A — module-level dict. Simplest, most explicit, zero extra imports._

## Technical Details

**Affected files:** `scripts/embed.py`

## Acceptance Criteria

- [ ] `TextEmbedding` constructed at most once per model name per process lifetime
- [ ] `lcc grep` response time does not include model load after first call in session
- [ ] `reindex_vault` runs without repeated model instantiation
- [ ] 148 tests still pass (mock `TextEmbedding` constructor call count where applicable)

## Work Log

- 2026-03-29 — Identified by performance-oracle

## Resources

- `scripts/embed.py:98–107` (`_fastembed_embed`)
- fastembed ONNX model cold-load benchmarks: 500ms–2s typical
