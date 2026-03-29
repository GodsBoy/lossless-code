---
status: pending
priority: p2
issue_id: "011"
tags: [code-review, performance, python]
dependencies: []
---

# Numpy Vector Search Uses Row-by-Row Loop — Should Use Matrix Multiply

## Problem Statement

`_vector_search_numpy` iterates all stored embeddings one by one, calling `np.frombuffer` and `np.dot` per row. The Python loop overhead and per-row buffer extraction become the bottleneck above ~50,000 messages. At 100,000 messages the loop-based approach pushes `lcc grep` past the 200ms interactive threshold.

## Findings

**Agent:** performance-oracle (P2)

- `embed.py:388–397` — per-row loop with `np.frombuffer` + `np.dot`
- Estimated latency: 5–15ms at 2,649 msgs (acceptable), 200–600ms at 100k (unacceptable), 2–6s at 1M
- Matrix multiply approach: 15–20ms at 100k (10–50× faster), viable to ~1M without ANN index

## Proposed Solutions

### Option A — Concatenate BLOBs + single matrix-vector product (Recommended)

```python
def _vector_search_numpy(query_vec, model_name, limit):
    try:
        import numpy as np
    except ImportError:
        return []
    import db as _db
    rows = _db.get_all_embeddings(model_name)
    if not rows:
        return []
    dims = len(query_vec)
    q = np.array(query_vec, dtype=np.float32)
    norm = np.linalg.norm(q)
    if norm == 0:
        return []
    q = q / norm

    # Filter valid rows, concatenate into matrix
    valid = [(r["message_id"], r["vector"]) for r in rows if len(r["vector"]) == dims * 4]
    if not valid:
        return []
    ids = [v[0] for v in valid]
    all_bytes = b"".join(v[1] for v in valid)
    matrix = np.frombuffer(all_bytes, dtype=np.float32).reshape(-1, dims)

    scores = matrix @ q                             # single BLAS call — O(N*D)
    top_k_idx = np.argpartition(scores, -min(limit, len(scores)))[-limit:]  # O(N), not O(N log N)
    top_k_idx = top_k_idx[np.argsort(scores[top_k_idx])[::-1]]

    return [(ids[i], float(scores[i])) for i in top_k_idx]
```

- Pros: 10–50× faster at scale; peak RAM same (still loads all BLOBs); O(N) top-k via argpartition
- Cons: Slightly more complex; requires all BLOB lengths to be valid (filtered)
- Effort: Medium (15 lines)
- Risk: Low — same correctness, better performance

## Recommended Action

_Option A — matrix multiply. The current approach is already correct; this is a drop-in performance improvement._

## Technical Details

**Affected files:** `scripts/embed.py`

## Acceptance Criteria

- [ ] `_vector_search_numpy` uses matrix multiply instead of row-by-row loop
- [ ] Results are identical for same inputs (cosine similarity ranking preserved)
- [ ] Invalid-length BLOBs are filtered before matrix construction
- [ ] Benchmark: 10k messages search completes in <50ms
- [ ] 148 tests still pass

## Work Log

- 2026-03-29 — Identified by performance-oracle

## Resources

- Scale threshold: numpy brute-force viable to ~50,000–100,000 messages at 200ms budget
- `vectorBackend: "auto"` config key anticipated ANN index for larger vaults
